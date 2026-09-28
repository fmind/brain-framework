#!/usr/bin/env python3
"""Collect one Google Calendar (for example `primary`) through the owner's gws CLI."""

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from datetime import time as midnight
from urllib.parse import quote
from zoneinfo import ZoneInfo

MAX_EVENTS = 10000
MAX_PAGES = 20
MAX_BYTES = 16 << 20
# BF record bounds: an id fits a BF address once percent-encoded; links and URLs are bounded single lines.
MAX_ID, MAX_ENCODED, MAX_REF, MAX_TITLE = 4096, 7988, 8192, 4096
# A record holds at most 1,000 links, and a mapped `many` field at most 1,000 values.
MAX_LINKS = 1000
# C0 and C1 control characters, which BF rejects in ids, titles, URLs and identities.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class InvalidError(ValueError):
    """A content-free diagnostic safe to show on stderr."""


def run(argv: list[str], limit: int, timeout: int) -> bytes:
    """Bound provider output while it runs; inherit Brain Framework's cancellable process group."""
    # Only literal provider commands reach this helper; no shell interprets argv.
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if child.stdout is None:
            raise ValueError("provider pipe is missing")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider exceeded its timeout")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > limit:
                        raise InvalidError("a gws page exceeds its byte limit")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def line(value: object, fallback: str) -> str:
    """One bounded title line: control characters become spaces and whitespace collapses."""
    return " ".join(CONTROL.sub(" ", str(value)).split())[:MAX_TITLE].rstrip() or fallback


def reference(value: object) -> str:
    """A URL or identity BF accepts, or nothing: an over-long or control-character value is dropped."""
    if isinstance(value, str) and value.strip() and len(value) <= MAX_REF and not CONTROL.search(value):
        return value
    return ""


def person(address: str) -> str:
    return reference("person:email/" + quote(address, safe="/:@+").replace("~", "%7E"))


def instant(value: object) -> str:
    """A provider timestamp with a timezone, or nothing: BF validates `attributes.updated` strictly."""
    try:
        return value if isinstance(value, str) and datetime.fromisoformat(value).tzinfo else ""
    except ValueError:
        return ""


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
            "fields": "kind,nextPageToken,timeZone,items(id,summary,description,start,end,status,htmlLink,updated,location,source,attachments(fileUrl,title),organizer(email),attendees(email),recurringEventId,originalStartTime)",
        }
        if token:
            params["pageToken"] = token
        payload = run(["gws", "calendar", "events", "list", "--params", json.dumps(params)], MAX_BYTES, 60)
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
                or len(quote(identifier, safe="/@:")) > MAX_ENCODED
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
            summary = line(event.get("summary") or "", "")
            if status == "cancelled":
                title = line("Cancelled: " + summary, "") if summary else "Cancelled calendar event"
            else:
                title = summary or "Untitled calendar event"
            lines = ["Event: " + title, "Status: " + status]
            event_end = event.get("end", {})
            updated = instant(event.get("updated"))
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
            if event.get("location"):
                lines.append("Location: " + event["location"])
            if event.get("description"):
                lines.append("Description:\n" + event["description"])
            references = []
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
                references.append(reference(source["url"]))
                lines.append(f"Source: {source.get('title', '')} {source['url']}")
            for attachment in attachments:
                if attachment.get("fileUrl"):
                    references.append(reference(attachment["fileUrl"]))
                    lines.append(f"Attachment: {attachment.get('title', '')} {attachment['fileUrl']}")
            key = quote(calendar, safe="@") + "/" + quote(identifier, safe="")
            organizer = event.get("organizer", {}).get("email", "").strip().lower()
            invited = {p["email"].strip().lower() for p in attendees if p.get("email")}
            organizer_refs = [ref for ref in map(person, [organizer] if organizer else []) if ref]
            attendee_refs = sorted(ref for ref in map(person, invited) if ref)
            if agenda_days:
                references.append(reference("calendar:" + key))
            # One invalid record fails the whole run: past 1,000 links, keep the organizer, source, attachments and
            # agenda link, then invitees in email order, and flag the cut. `participants` keeps every address.
            linked = list(dict.fromkeys(filter(None, [*organizer_refs, *references, *participant_refs])))
            truncated = max(len(linked), len(participant_refs), len(attendee_refs)) > MAX_LINKS
            records.append(
                {
                    "id": identifier,
                    "title": title,
                    "time": when,
                    "text": "\n".join(lines),
                    "url": reference(event.get("htmlLink", "")),
                    "links": sorted(linked[:MAX_LINKS]),
                    "aliases": list(filter(None, [reference(("agenda:" if agenda_days else "calendar:") + key)])),
                    "attributes": {
                        "organizer_refs": organizer_refs,
                        "attendee_refs": attendee_refs[:MAX_LINKS],
                        "participant_refs": participant_refs[:MAX_LINKS],
                        "start": event_start,
                        "end": event_end,
                        # `updated` is BF's reserved provider-modification time: only a valid timestamp maps to it.
                        "updated": updated,
                        "location": event.get("location", ""),
                        "source": source,
                        "attachments": attachments,
                        "status": status,
                        "timezone": timezone,
                        "all_day": bool(event_start.get("date")),
                        "participants": emails,
                        **({"participants_truncated": True} if truncated else {}),
                    },
                }
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calendar")
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--agenda-days", type=int, default=0)
    arguments = parser.parse_args()
    try:
        value = collect(arguments.calendar, arguments.start, arguments.end, arguments.agenda_days)
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise InvalidError("normalized calendar exceeds 16 MiB; narrow the window")
        print(payload)
    except InvalidError as error:
        print(f"Calendar collection failed: {error}.", file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Calendar collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
