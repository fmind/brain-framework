#!/usr/bin/env python3
"""Collect reviewed Cloud Audit metadata across every readable visible project."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from typing import Any, NoReturn
from urllib.parse import quote

MAX_OUTPUT_BYTES = 64 << 20
MAX_PROJECTS = 1_000
MAX_PROVIDER_BYTES = 64 << 20
MAX_RECORDS = 100_000
ASCII_DOWNCASE = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def invoke(arguments: list[str]) -> bytes:
    with subprocess.Popen(
        ["gcloud", *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        if process.stdout is None:  # pragma: no cover
            raise RuntimeError("cannot capture gcloud output")
        raw = process.stdout.read(MAX_PROVIDER_BYTES + 1)
        if len(raw) > MAX_PROVIDER_BYTES:
            process.kill()
            process.wait()
            raise RuntimeError("gcloud output exceeds the safety limit")
        if process.wait() != 0:
            raise RuntimeError("gcloud command failed")
    return raw


def decode_array(raw: bytes) -> list[Any]:
    def reject_constant(_value: str) -> None:
        raise ValueError

    value = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, list):
        raise TypeError
    return value


def compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def identity(value: str) -> str:
    return quote(value, safe=":/@+").replace("~", "%7E")


def projected(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise TypeError
    resource = record.get("resource") or {}
    proto = record.get("protoPayload") or {}
    operation = record.get("operation") or {}
    if not all(isinstance(value, dict) for value in (resource, proto, operation)):
        raise ValueError
    labels = resource.get("labels") or {}
    auth = proto.get("authenticationInfo") or {}
    status = proto.get("status") or {}
    authorization = proto.get("authorizationInfo") or []
    if not all(isinstance(value, dict) for value in (labels, auth, status)):
        raise ValueError
    if not isinstance(authorization, list) or any(
        not isinstance(item, dict) for item in authorization
    ):
        raise ValueError
    project = labels.get("project_id", "unknown")
    insert_id = record.get("insertId")
    timestamp = record.get("timestamp")
    if not all(
        isinstance(value, str) and value for value in (project, insert_id, timestamp)
    ):
        raise ValueError
    email = auth.get("principalEmail")
    return compact(
        {
            "uid": f"{project}@{insert_id}@{timestamp}",
            **{
                key: record.get(key)
                for key in (
                    "insertId",
                    "timestamp",
                    "receiveTimestamp",
                    "severity",
                    "logName",
                )
            },
            "resource": compact(
                {
                    "type": resource.get("type"),
                    "labels": compact(
                        {
                            key: labels.get(key)
                            for key in (
                                "project_id",
                                "location",
                                "zone",
                                "cluster_name",
                                "namespace_name",
                            )
                        }
                    ),
                }
            ),
            "protoPayload": compact(
                {
                    **{
                        key: proto.get(key)
                        for key in ("serviceName", "methodName", "resourceName")
                    },
                    "authenticationInfo": compact(
                        {
                            **{
                                key: auth.get(key)
                                for key in (
                                    "principalEmail",
                                    "principalSubject",
                                    "serviceAccountKeyName",
                                )
                            },
                            "principal_uri": (
                                f"person:email/{identity(email.translate(ASCII_DOWNCASE))}"
                                if isinstance(email, str) and email
                                else None
                            ),
                        }
                    ),
                    "authorizationInfo": [
                        compact(
                            {
                                key: item.get(key)
                                for key in ("resource", "permission", "granted")
                            }
                        )
                        for item in authorization
                    ],
                    "status": compact({"code": status.get("code")}),
                }
            ),
            "operation": compact(
                {key: operation.get(key) for key in ("id", "producer", "first", "last")}
            ),
        }
    )


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("gcloud-audit-projects-json.py (fkf base helper)\n")
        return 0
    class Arguments(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            self.exit(2, "gcloud-audit-projects-json.py: invalid arguments; see --help\n")

    parser = Arguments(description=__doc__)
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--exclude-project", action="append", default=[])
    parser.add_argument("--exclude-project-prefix", action="append", default=[])
    selected = parser.parse_args(arguments)
    start, end = selected.start, selected.end
    try:
        for value in (start, end):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})", value) is None:
                raise ValueError
        if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
            raise ValueError
        exclusions = selected.exclude_project + selected.exclude_project_prefix
        if any(re.fullmatch(r"[a-z][a-z0-9-]*", value) is None for value in exclusions):
            raise ValueError
    except ValueError:
        sys.stderr.write("gcloud-audit-projects-json.py: invalid UTC window or project exclusion\n")
        return 2
    filters = [f"NOT projectId = {value}" for value in selected.exclude_project]
    filters += [f"NOT projectId ~ ^{value}" for value in selected.exclude_project_prefix]
    try:
        projects = decode_array(
            invoke(
                [
                    "projects",
                    "list",
                    f"--limit={MAX_PROJECTS + 1}",
                    "--format=json",
                    *(["--filter=" + " AND ".join(filters)] if filters else []),
                ]
            )
        )
        if len(projects) > MAX_PROJECTS:
            raise ValueError
        project_ids = []
        for project in projects:
            if not isinstance(project, dict):
                raise TypeError
            project_id = project.get("projectId")
            if not isinstance(project_id, str) or not project_id:
                raise ValueError
            project_ids.append(project_id)
        if len(project_ids) != len(set(project_ids)):
            raise ValueError
    except (
        OSError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        sys.stderr.write(
            "gcloud-audit-projects-json.py: gcloud returned an invalid project list\n"
        )
        return 1

    filter_value = (
        f'timestamp>="{start}" AND timestamp<"{end}" '
        'AND log_id("cloudaudit.googleapis.com/activity") '
        "AND protoPayload.authenticationInfo.principalEmail:* "
        'AND NOT protoPayload.authenticationInfo.principalEmail:"system:" '
        'AND NOT protoPayload.authenticationInfo.principalEmail:"container-engine-robot" '
        'AND NOT protoPayload.authenticationInfo.principalEmail:"kubelet-nodepool-bootstrap"'
    )
    records: dict[str, dict[str, Any]] = {}
    # Account for the array delimiters and trailing newline before retaining records.
    encoded_bytes = 3
    try:
        for project_id in project_ids:
            values = decode_array(
                invoke(
                    [
                        "logging",
                        "read",
                        filter_value,
                        f"--project={project_id}",
                        f"--limit={MAX_RECORDS + 1}",
                        "--format=json",
                    ]
                )
            )
            if len(values) > MAX_RECORDS:
                raise ValueError
            for value in values:
                record = projected(value)
                uid = str(record["uid"])
                if uid in records:
                    continue
                encoded = json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode()
                separator_bytes = 1 if records else 0
                if (
                    len(records) >= MAX_RECORDS
                    or encoded_bytes + separator_bytes + len(encoded) > MAX_OUTPUT_BYTES
                ):
                    raise ValueError
                records[uid] = record
                encoded_bytes += separator_bytes + len(encoded)
        output = json.dumps(
            sorted(
                records.values(),
                key=lambda record: (record["timestamp"], record["uid"]),
            ),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (
        OSError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        # A partial project set would falsely claim complete account coverage.
        sys.stderr.write(
            "gcloud-audit-projects-json.py: cannot read one visible project's audit logs\n"
        )
        return 1
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
