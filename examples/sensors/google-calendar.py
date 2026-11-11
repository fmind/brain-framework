#!/usr/bin/env python3
"""Collect one Google Calendar (for example `primary`) through the owner's gws CLI."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from datetime import time as midnight
from urllib.parse import quote
from zoneinfo import ZoneInfo

import _sensor

MAX_EVENTS = 10000
MAX_PAGES = 20
MAX_BYTES = 16 << 20
InvalidError = _sensor.InvalidError


def optional_instant(value: object) -> str:
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
        payload = _sensor.run(
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
                or _sensor.CONTROL.search(identifier)
                or len(identifier) > _sensor.MAX_ID
                or len(quote(identifier, safe="/@:")) > _sensor.MAX_ENCODED_ID
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
            summary = _sensor.line(event.get("summary") or "")
            if status == "cancelled":
                title = _sensor.line("Cancelled: " + summary) if summary else "Cancelled calendar event"
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
            location = _sensor.redact(event.get("location") or "")
            if location:
                lines.append("Location: " + location)
            if event.get("description"):
                lines.append("Description:\n" + _sensor.redact(event["description"]))
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
            participant_refs = [ref for ref in map(_sensor.person, emails) if ref]
            if emails:
                lines.append("Participants: " + ", ".join(emails))
            source = event.get("source", {})
            if source.get("url"):
                references.append(_sensor.reference(source["url"]))
                lines.append(f"Source: {source.get('title', '')} {source['url']}")
            for attachment in attachments:
                if attachment.get("fileUrl"):
                    references.append(_sensor.reference(attachment["fileUrl"]))
                    lines.append(f"Attachment: {attachment.get('title', '')} {attachment['fileUrl']}")
            key = quote(calendar, safe="@") + "/" + quote(identifier, safe="")
            organizer = event.get("organizer", {}).get("email", "").strip().lower()
            invited = {p["email"].strip().lower() for p in attendees if p.get("email")}
            organizer_refs = [ref for ref in map(_sensor.person, [organizer] if organizer else []) if ref]
            attendee_refs = sorted(ref for ref in map(_sensor.person, invited) if ref)
            if agenda_days:
                references.append(_sensor.reference("calendar:" + key))
            # One invalid record fails the whole run: past 1,000 links, keep the organizer, source, attachments and
            # agenda link, then invitees in email order, and flag the cut. `participants` keeps every address.
            linked = list(dict.fromkeys(filter(None, [*organizer_refs, *references, *participant_refs])))
            truncated = max(len(linked), len(participant_refs), len(attendee_refs)) > _sensor.MAX_LINKS
            alias = _sensor.reference(("agenda:" if agenda_days else "calendar:") + key)
            records.append(
                _sensor.record(
                    identifier,
                    title,
                    time=when,
                    text="\n".join(lines),
                    url=str(event.get("htmlLink", "")),
                    links=linked,
                    aliases=[alias] if alias else (),
                    attributes={
                        "organizer_refs": organizer_refs,
                        "attendee_refs": attendee_refs,
                        "participant_refs": participant_refs,
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
    _sensor.cli(
        lambda: _sensor.emit(
            collect(arguments.calendar, arguments.start, arguments.end, arguments.agenda_days), max_bytes=MAX_BYTES
        ),
        "Calendar collection failed",
        "check gws authentication and the requested window",
    )


if __name__ == "__main__":
    main()
