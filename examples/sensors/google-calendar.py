#!/usr/bin/env python3
"""Collect one Google Calendar (for example `primary`) through the owner's gws CLI."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as midnight
from urllib.parse import quote
from zoneinfo import ZoneInfo

MAX_EVENTS = 10000
MAX_PAGES = 20
MAX_BYTES = 16 << 20
MAX_ID = 4096
MAX_ENCODED_ID = 7988
MAX_REF = 8192
MAX_LINKS = 1000
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")
_SECRET_PATTERNS = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ASIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|xox[baprs]-[0-9A-Za-z-]+"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
    r"|(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"
    r"|(?i:\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+)"
    r"|(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{12,}))"
)


class InvalidError(ValueError):
    """Provider output or inputs violated the expected contract."""


class ProviderError(Exception):
    """Provider subprocess failed, timed out, or could not start."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def run(argv: Sequence[str], *, timeout: float, max_bytes: int) -> bytes:
    """Execute one provider CLI from / with bounded output and process-group reaping."""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProviderError(f"cannot start {argv[0]}") from error
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        raise RuntimeError("subprocess pipes missing")
    out_fd, err_fd = stdout.fileno(), stderr.fileno()
    chunks: list[bytes] = []
    overflow = False

    def drain_out() -> None:
        nonlocal overflow
        total = 0
        while data := os.read(out_fd, 65536):
            total += len(data)
            if total > max_bytes:
                overflow = True
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                return
            chunks.append(data)

    def drain_err() -> None:
        while os.read(err_fd, 65536):
            pass

    t_out = threading.Thread(target=drain_out, daemon=True)
    t_err = threading.Thread(target=drain_err, daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout
    try:
        while t_out.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout")
            t_out.join(timeout=min(remaining, 0.05))
        try:
            remaining = max(0.0, deadline - time.monotonic())
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout") from None
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stdout.close()
        stderr.close()
    if overflow:
        raise InvalidError(f"{argv[0]} output exceeds {max_bytes} bytes")
    if code != 0:
        raise ProviderError(f"{argv[0]} exited with status {code}", status=code)
    return b"".join(chunks)


def line(value: str, fallback: str = "") -> str:
    cleaned = " ".join(CONTROL.sub(" ", value).split())
    return (cleaned[:4096] or fallback)[:4096] if (cleaned or fallback) else ""


def reference(value: str) -> str:
    if (
        not value
        or len(value) > MAX_REF
        or any(c.isspace() for c in value)
        or CONTROL.search(value)
        or not IDENTITY.fullmatch(value)
    ):
        return ""
    return value


def person(email: str) -> str:
    cleaned = email.strip().lower()
    return reference("person:email/" + quote(cleaned, safe="@.")) if "@" in cleaned else ""


def iso_utc(value: str) -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise InvalidError("timestamps require timezones")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def redact(text: str) -> str:
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


def emit(records: list[dict[str, object]], *, max_bytes: int = MAX_BYTES) -> None:
    payload = (json.dumps(records, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise InvalidError(f"output exceeds {max_bytes} bytes")
    sys.stdout.buffer.write(payload)


def cli(action: Callable[[], None], summary: str, hint: str) -> None:
    try:
        action()
    except (InvalidError, ProviderError) as error:
        sys.stderr.write(f"{summary}: {error}.\n")
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, LookupError, ArithmeticError, RecursionError):
        sys.stderr.write(f"{summary}; {hint}.\n")
        sys.exit(1)


def optional_instant(value: object) -> str:
    """A provider timestamp with a timezone, or nothing: BF validates `attributes.updated` strictly."""
    try:
        return iso_utc(value) if isinstance(value, str) and value else ""
    except (ValueError, InvalidError):
        return ""


def make_record(
    identifier: str,
    title: str,
    *,
    item_type: str,
    time_value: str,
    text: str,
    url: str,
    links: Sequence[str],
    aliases: Sequence[str],
    relations: dict[str, list[str]],
    attributes: dict[str, object],
) -> dict[str, object]:
    out: dict[str, object] = {
        "id": identifier,
        "title": line(title, identifier),
        "@type": item_type,
        "text": text,
    }
    if time_value:
        out["time"] = iso_utc(time_value)
    if valid_url := reference(url):
        out["url"] = valid_url
    valid_links = sorted({ref for raw in links if (ref := reference(raw))})[:MAX_LINKS]
    if valid_links:
        out["links"] = valid_links
    valid_aliases = sorted({a for a in aliases if reference(a)})
    if valid_aliases:
        out["aliases"] = valid_aliases
    cleaned = {k: list(dict.fromkeys(r for r in v if reference(r)))[:MAX_LINKS] for k, v in relations.items()}
    nonempty = {k: v for k, v in cleaned.items() if v}
    if nonempty:
        out["relations"] = nonempty
    if attributes:
        out["attributes"] = attributes
    return out


def collect(calendar: str, start: str, end: str, agenda_days: int = 0) -> list[dict[str, object]]:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish or not 0 <= agenda_days <= 366:
        raise InvalidError("START and END need timezones, with START before END, and an agenda of 0-366 days")
    if agenda_days:
        # Include today's already-started/all-day events in every timezone. Its separate snapshot source
        # removes events that leave this range, such as past or rescheduled ones.
        start = (finish - timedelta(days=2)).isoformat()
        end = (finish + timedelta(days=agenda_days)).isoformat()
    records: list[dict[str, object]] = []
    token = ""
    seen = set()
    identifiers: set[str] = set()
    for _ in range(MAX_PAGES):
        # https://developers.google.com/workspace/calendar/api/v3/reference/events/list
        params = {
            "calendarId": calendar,
            "timeMin": start,
            "timeMax": end,
            "singleEvents": True,
            # Cancellations overwrite records collected earlier, titled as cancelled. In the agenda this keeps a
            # cancelled meeting visible as cancelled, and a cancellation never shrinks the snapshot.
            "showDeleted": True,
            "maxResults": 500,
            "fields": (
                "kind,nextPageToken,timeZone,items(id,summary,description,start,end,status,"
                "htmlLink,updated,location,source,attachments(fileUrl,title),organizer(email),"
                "attendees(email),recurringEventId,originalStartTime)"
            ),
        }
        if token:
            params["pageToken"] = token
        payload = run(
            ["gws", "calendar", "events", "list", "--params", json.dumps(params)], timeout=60, max_bytes=MAX_BYTES
        )
        page = json.loads(payload)
        if (
            not isinstance(page, dict)
            or page.get("kind") != "calendar#events"
            or not isinstance(page.get("items", []), list)
        ):
            raise InvalidError("gws returned an unexpected Calendar response")
        for event in page.get("items", []):
            if not isinstance(event, dict):
                raise InvalidError("gws returned an invalid event")
            identifier = event.get("id")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in identifiers
                or CONTROL.search(identifier)
                or len(identifier) > MAX_ID
                or len(quote(identifier, safe="/@:")) > MAX_ENCODED_ID
            ):
                raise InvalidError("gws returned an invalid, duplicate or over-long event id")
            identifiers.add(identifier)
            if len(identifiers) > MAX_EVENTS:
                raise InvalidError(f"the window has more than {MAX_EVENTS} events; narrow it")
            metadata = ("start", "end", "organizer", "source", "originalStartTime")
            if any(not isinstance(event.get(key, {}), dict) for key in metadata):
                raise InvalidError("gws returned invalid event metadata")
            attachments = event.get("attachments", [])
            if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
                raise InvalidError("gws returned invalid event attachments")
            # A cancelled instance of a recurring event may carry only its original start.
            event_start = event.get("start") or event.get("originalStartTime", {})
            when = event_start.get("dateTime", "")
            status = event.get("status", "unknown")
            summary = line(event.get("summary") or "")
            if status == "cancelled":
                title = line("Cancelled: " + summary) if summary else "Cancelled calendar event"
            else:
                title = summary or "Untitled calendar event"
            lines = ["Event: " + title, "Status: " + status]
            event_end = event.get("end", {})
            updated = optional_instant(event.get("updated"))
            timezone = event_start.get("timeZone") or page.get("timeZone", "")
            if event_start.get("date"):
                if not timezone:
                    raise InvalidError("an all-day event has no calendar timezone")
                # This is the documented all-day boundary, not an invented appointment time.
                boundary = datetime.combine(date.fromisoformat(event_start["date"]), midnight(), ZoneInfo(timezone))
                when = boundary.astimezone(UTC).isoformat()
                lines.append(f"All-day date: {event_start['date']} ({timezone})")
            elif when:
                lines.append("Start: " + when)
            if event_end.get("dateTime") or event_end.get("date"):
                lines.append("End (exclusive): " + (event_end.get("dateTime") or event_end["date"]))
            if timezone:
                lines.append("Timezone: " + timezone)
            if updated:
                # Modification time describes the observation; event time remains its start.
                lines.append("Provider updated: " + updated)
            location = redact(event.get("location") or "")
            if location:
                lines.append("Location: " + location)
            if event.get("description"):
                lines.append("Description:\n" + redact(event["description"]))
            references: list[str] = []
            attendees = event.get("attendees", [])
            if not isinstance(attendees, list):
                raise InvalidError("gws returned invalid attendees")
            people = [event.get("organizer", {}), *attendees]
            if any(not isinstance(p, dict) or not isinstance(p.get("email", ""), str) for p in people):
                raise InvalidError("gws returned invalid participants")
            emails = sorted({p["email"].strip().lower() for p in people if p.get("email")})
            if any("@" not in address for address in emails):
                raise InvalidError("gws returned an invalid participant email")
            participant_refs = [ref for ref in map(person, emails) if ref]
            if emails:
                lines.append("Participants: " + ", ".join(emails))
            source = event.get("source", {})
            if source.get("url"):
                if ref := reference(source["url"]):
                    references.append(ref)
                lines.append(f"Source: {source.get('title', '')} {source['url']}")
            for attachment in attachments:
                if attachment.get("fileUrl"):
                    if ref := reference(attachment["fileUrl"]):
                        references.append(ref)
                    lines.append(f"Attachment: {attachment.get('title', '')} {attachment['fileUrl']}")
            key = quote(calendar, safe="@") + "/" + quote(identifier, safe="")
            organizer = event.get("organizer", {}).get("email", "").strip().lower()
            invited = {p["email"].strip().lower() for p in attendees if p.get("email")}
            organizer_refs = [ref for ref in map(person, [organizer] if organizer else []) if ref]
            attendee_refs = sorted(ref for ref in map(person, invited) if ref)
            if agenda_days and (ref := reference("calendar:" + key)):
                references.append(ref)
            # Attachments and agenda references go in links; organizer, attendee and participant identities become typed
            # relations (each bounded to BF's 1,000 targets). `participants` keeps every raw address.
            truncated = max(len(references), len(participant_refs), len(attendee_refs)) > MAX_LINKS
            alias = reference(("agenda:" if agenda_days else "calendar:") + key)
            records.append(
                make_record(
                    identifier,
                    title,
                    item_type="Event",
                    time_value=when,
                    text="\n".join(lines),
                    url=str(event.get("htmlLink", "")),
                    links=references,
                    aliases=[alias] if alias else (),
                    relations={
                        "organizer": organizer_refs,
                        "attendee": attendee_refs,
                        "participant": participant_refs,
                    },
                    attributes={
                        "start": event_start,
                        "end": event_end,
                        # `updated` is BF's reserved provider-modification time: only a valid timestamp maps to it.
                        "updated": updated,
                        "location": location,
                        "source": source,
                        "attachments": attachments,
                        "status": status,
                        "timezone": timezone,
                        "all_day": bool(event_start.get("date")),
                        "participants": emails,
                        **({"participants_truncated": True} if truncated else {}),
                    },
                )
            )
        token = page.get("nextPageToken", "")
        if not isinstance(token, str):
            raise InvalidError("gws returned an invalid continuation token")
        if not token:
            return records
        if token in seen:
            raise InvalidError("gws repeated a continuation token")
        seen.add(token)
    raise InvalidError(f"the window needs more than {MAX_PAGES} pages; narrow it")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calendar")
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--agenda-days", type=int, default=0)
    arguments = parser.parse_args()
    cli(
        lambda: emit(
            collect(arguments.calendar, arguments.start, arguments.end, arguments.agenda_days), max_bytes=MAX_BYTES
        ),
        "Calendar collection failed",
        "check gws authentication and the requested window",
    )


if __name__ == "__main__":
    main()
