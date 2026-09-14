#!/usr/bin/env python3
"""Collect the owner's Google Contacts through the gws CLI.

Contacts are a current list rather than dated events, so the START and END window arguments FKF passes are
accepted and ignored; ordinary retrieval keeps the latest capture of each contact.
"""

import json
import subprocess
import sys
import tempfile
from urllib.parse import quote

PAGE_BYTES = 16 << 20
PAGE_LIMIT = 20
PERSON_FIELDS = "names,emailAddresses,organizations,phoneNumbers"


def call(arguments: list[str]) -> dict[str, object]:
    with tempfile.TemporaryFile() as output:
        subprocess.run(["gws", *arguments], check=True, stdout=output, stderr=subprocess.DEVNULL, timeout=120)
        output.seek(0)
        payload = output.read(PAGE_BYTES + 1)
    if len(payload) > PAGE_BYTES:
        raise ValueError("provider page exceeds 16 MiB")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("unexpected provider response")
    return value


def entries(person: dict[str, object], field: str) -> list[dict[str, object]]:
    values = person.get(field) or []
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise ValueError("unexpected person field")
    return values


def tidy(value: object) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def collect() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    token = ""
    for _ in range(PAGE_LIMIT):
        # https://developers.google.com/people/api/rest/v1/people.connections/list
        params: dict[str, object] = {"resourceName": "people/me", "pageSize": 200, "personFields": PERSON_FIELDS}
        if token:
            params["pageToken"] = token
        page = call(["people", "people", "connections", "list", "--params", json.dumps(params)])
        connections = page.get("connections", [])
        if not isinstance(connections, list) or any(not isinstance(item, dict) for item in connections):
            raise ValueError("unexpected connections listing")
        for person in connections:
            identifier = person.get("resourceName")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("contact without a resource name")
            if identifier in seen:
                raise ValueError("connections listing repeated a contact")
            seen.add(identifier)
            names = [tidy(item.get("displayName")) for item in entries(person, "names")]
            emails = [tidy(item.get("value")).lower() for item in entries(person, "emailAddresses")]
            emails = [address for address in emails if "@" in address]
            organizations = entries(person, "organizations")
            phones = [tidy(item.get("value")) for item in entries(person, "phoneNumbers")]
            title = next((name for name in names if name), None) or next(iter(emails), None) or identifier
            lines = ["Contact: " + title]
            lines.extend("Email: " + address for address in emails)
            for organization in organizations:
                if tidy(organization.get("name")):
                    lines.append("Organization: " + tidy(organization.get("name")))
                if tidy(organization.get("title")):
                    lines.append("Title: " + tidy(organization.get("title")))
            lines.extend("Phone: " + phone for phone in phones if phone)
            records.append(
                {
                    "id": identifier,
                    "title": title,
                    "text": "\n".join(lines),
                    "links": sorted(
                        {"person:email/" + quote(address, safe="/:@+").replace("~", "%7E") for address in emails}
                    ),
                    "attributes": {
                        "names": names,
                        "emailAddresses": emails,
                        "organizations": [
                            {key: tidy(item.get(key)) for key in ("name", "title", "department") if tidy(item.get(key))}
                            for item in organizations
                        ],
                        "phoneNumbers": [phone for phone in phones if phone],
                    },
                }
            )
        token = str(page.get("nextPageToken") or "")
        if not token:
            return records
        if token in seen:
            raise ValueError("connections pagination repeated a token")
        seen.add(token)
    raise ValueError("connections pagination exceeded its page limit")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("expected START and END arguments")
        value = collect()
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > PAGE_BYTES:
            raise ValueError("normalized contacts exceed 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError:
        print("Contacts collection failed; check gws authentication.", file=sys.stderr)
        sys.exit(1)
