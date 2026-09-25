#!/usr/bin/env python3
"""Collect one Google Calendar (for example `primary`) through the owner's gws CLI."""

import argparse
import json
import os
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from datetime import time as midnight
from urllib.parse import quote
from zoneinfo import ZoneInfo


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
                        raise ValueError("provider output exceeds its byte limit")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def collect(calendar: str, start: str, end: str, agenda_days: int = 0) -> list[dict[str, object]]:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish or not 0 <= agenda_days <= 366:
        raise ValueError("invalid calendar window or agenda horizon")
    if agenda_days:
        # Include today's already-started/all-day events in every timezone; a
        # separate snapshot source removes cancelled or rescheduled appointments.
        start = (finish - timedelta(days=2)).isoformat()
        end = (finish + timedelta(days=agenda_days)).isoformat()
    records: list[dict[str, object]] = []
    token = ""
    seen = set()
    for _ in range(20):
        # https://developers.google.com/workspace/calendar/api/v3/reference/events/list
        params = {
            "calendarId": calendar,
            "timeMin": start,
            "timeMax": end,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 500,
            "fields": "kind,nextPageToken,timeZone,items(id,summary,description,start,end,status,htmlLink,updated,location,source,attachments(fileUrl,title),organizer(email),attendees(email))",
        }
        if token:
            params["pageToken"] = token
        payload = run(["gws", "calendar", "events", "list", "--params", json.dumps(params)], 16 << 20, 60)
        page = json.loads(payload)
        if not isinstance(page, dict) or page.get("kind") != "calendar#events":
            raise ValueError("unexpected Calendar collection response")
        for event in page.get("items", []):
            identifier = event["id"]
            begin = event.get("start", {})
            when = begin.get("dateTime", "")
            status = event.get("status", "unknown")
            title = event.get("summary") or (
                "Cancelled calendar event" if status == "cancelled" else "Untitled calendar event"
            )
            lines = ["Event: " + title, "Status: " + status]
            finish = event.get("end", {})
            updated = event.get("updated", "")
            timezone = begin.get("timeZone") or page.get("timeZone", "")
            if begin.get("date"):
                if not timezone:
                    raise ValueError("all-day event has no calendar timezone")
                # This is the documented all-day boundary, not an invented appointment time.
                boundary = datetime.combine(date.fromisoformat(begin["date"]), midnight(), ZoneInfo(timezone))
                when = boundary.astimezone(UTC).isoformat()
                lines.append(f"All-day date: {begin['date']} ({timezone})")
            elif when:
                lines.append("Start: " + when)
            if finish.get("dateTime") or finish.get("date"):
                lines.append("End (exclusive): " + (finish.get("dateTime") or finish["date"]))
            if timezone:
                lines.append("Timezone: " + timezone)
            if updated:
                # Modification time describes the observation; event time remains its start.
                lines.append("Provider updated: " + updated)
            if event.get("location"):
                lines.append("Location: " + event["location"])
            if event.get("description"):
                lines.append("Description:\n" + event["description"])
            links = []
            attendees = event.get("attendees", [])
            if not isinstance(attendees, list):
                raise ValueError("invalid attendees")
            people = [event.get("organizer", {}), *attendees]
            if any(not isinstance(person, dict) or not isinstance(person.get("email", ""), str) for person in people):
                raise ValueError("invalid participants")
            emails = sorted({person["email"].strip().lower() for person in people if person.get("email")})
            if any("@" not in address for address in emails):
                raise ValueError("invalid participant email")
            links.extend("person:email/" + quote(address, safe="/:@+").replace("~", "%7E") for address in emails)
            if emails:
                lines.append("Participants: " + ", ".join(emails))
            source = event.get("source", {})
            if source.get("url"):
                links.append(source["url"])
                lines.append(f"Source: {source.get('title', '')} {source['url']}")
            attachments = event.get("attachments", [])
            for attachment in attachments:
                if attachment.get("fileUrl"):
                    links.append(attachment["fileUrl"])
                    lines.append(f"Attachment: {attachment.get('title', '')} {attachment['fileUrl']}")
            records.append(
                {
                    "id": identifier,
                    "title": title,
                    "time": when,
                    "text": "\n".join(lines),
                    "url": event.get("htmlLink", ""),
                    "links": sorted(set(links + ([f"calendar:{calendar}/{identifier}"] if agenda_days else []))),
                    "aliases": [f"{'agenda' if agenda_days else 'calendar'}:{calendar}/{identifier}"],
                    "attributes": {
                        "organizer_refs": [
                            "person:email/" + quote(person["email"].strip().lower(), safe="/:@+").replace("~", "%7E")
                            for person in [event.get("organizer", {})]
                            if person.get("email")
                        ],
                        "attendee_refs": sorted(
                            {
                                "person:email/"
                                + quote(person["email"].strip().lower(), safe="/:@+").replace("~", "%7E")
                                for person in attendees
                                if person.get("email")
                            }
                        ),
                        "participant_refs": [link for link in links if link.startswith("person:")],
                        "start": begin,
                        "end": finish,
                        "updated": updated,
                        "location": event.get("location", ""),
                        "source": source,
                        "attachments": attachments,
                        "status": status,
                        "timezone": timezone,
                        "all_day": bool(begin.get("date")),
                        "participants": emails,
                    },
                }
            )
        token = page.get("nextPageToken", "")
        if not token:
            return records
        if token in seen:
            raise ValueError("calendar pagination repeated a token")
        seen.add(token)
    raise ValueError("calendar pagination exceeded 20 pages")


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
        if len(payload.encode()) > 16 << 20:
            raise ValueError("normalized calendar exceeds 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, IndexError:
        print("Calendar collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
