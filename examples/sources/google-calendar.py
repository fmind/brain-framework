#!/usr/bin/env python3
"""Collect one Google Calendar (for example `primary`) through the owner's gws CLI."""

import json
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime, time
from urllib.parse import quote
from zoneinfo import ZoneInfo


def collect(calendar: str, start: str, end: str) -> list[dict[str, object]]:
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
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                ["gws", "calendar", "events", "list", "--params", json.dumps(params)],
                check=True,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            output.seek(0)
            payload = output.read((16 << 20) + 1)
        if len(payload) > 16 << 20:
            raise ValueError("calendar page exceeds 16 MiB")
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
                boundary = datetime.combine(date.fromisoformat(begin["date"]), time(), ZoneInfo(timezone))
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
                    "links": sorted(set(links)),
                    "aliases": [f"calendar:{calendar}/{identifier}"],
                    "attributes": {
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
    try:
        value = collect(sys.argv[1], sys.argv[2], sys.argv[3])
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 16 << 20:
            raise ValueError("normalized calendar exceeds 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, IndexError:
        print("Calendar collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
