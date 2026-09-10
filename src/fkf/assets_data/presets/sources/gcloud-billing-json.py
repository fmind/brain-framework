#!/usr/bin/env python3
"""Collect bounded project-to-billing-account metadata for visible accounts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

MAX_ACCOUNTS = 100
MAX_OUTPUT_BYTES = 64 << 20
MAX_PROJECTS_PER_ACCOUNT = 10_000
MAX_TOTAL_PROJECTS = 100_000
MAX_PROVIDER_BYTES = 64 << 20
ACCOUNT_NAME = re.compile(r"^billingAccounts/[A-Za-z0-9-]+$")


def invoke(arguments: list[str]) -> list[Any]:
    with subprocess.Popen(
        ["gcloud", *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        if process.stdout is None:  # pragma: no cover
            raise RuntimeError
        raw = process.stdout.read(MAX_PROVIDER_BYTES + 1)
        if len(raw) > MAX_PROVIDER_BYTES:
            process.kill()
            process.wait()
            raise ValueError
        if process.wait() != 0:
            raise RuntimeError

    def reject_constant(_value: str) -> None:
        raise ValueError

    value = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, list):
        raise TypeError
    return value


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("gcloud-billing-json.py (fkf base helper)\n")
        return 0
    if arguments:
        sys.stderr.write("usage: gcloud-billing-json.py\n")
        return 2
    try:
        accounts = invoke(
            [
                "billing",
                "accounts",
                "list",
                f"--limit={MAX_ACCOUNTS + 1}",
                "--format=json(name)",
            ]
        )
    except RuntimeError:
        sys.stderr.write("gcloud-billing-json.py: cannot list billing accounts\n")
        return 1
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        sys.stderr.write(
            "gcloud-billing-json.py: billing accounts have an unexpected shape\n"
        )
        return 1
    names: list[str] = []
    try:
        for account in accounts:
            if not isinstance(account, dict):
                raise TypeError
            name = account.get("name")
            if not isinstance(name, str) or ACCOUNT_NAME.fullmatch(name) is None:
                raise ValueError
            names.append(name.removeprefix("billingAccounts/"))
    except (TypeError, ValueError):
        sys.stderr.write(
            "gcloud-billing-json.py: billing accounts have an unexpected shape\n"
        )
        return 1
    if len(names) > MAX_ACCOUNTS:
        sys.stderr.write(
            f"gcloud-billing-json.py: more than {MAX_ACCOUNTS} billing accounts; refusing a prefix\n"
        )
        return 1

    records: dict[str, dict[str, Any]] = {}
    # Account for the array delimiters and trailing newline before retaining records.
    encoded_bytes = 3
    for account in names:
        try:
            projects = invoke(
                [
                    "billing",
                    "projects",
                    "list",
                    f"--billing-account={account}",
                    f"--limit={MAX_PROJECTS_PER_ACCOUNT + 1}",
                    "--format=json(projectId,billingAccountName,billingEnabled)",
                ]
            )
        except RuntimeError:
            sys.stderr.write(
                "gcloud-billing-json.py: cannot list projects for one visible billing account\n"
            )
            return 1
        except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            sys.stderr.write(
                "gcloud-billing-json.py: project billing links have an unexpected shape\n"
            )
            return 1
        if len(projects) > MAX_PROJECTS_PER_ACCOUNT:
            sys.stderr.write(
                "gcloud-billing-json.py: one account has more than "
                f"{MAX_PROJECTS_PER_ACCOUNT} projects; refusing a prefix\n"
            )
            return 1
        for project in projects:
            if not isinstance(project, dict):
                sys.stderr.write(
                    "gcloud-billing-json.py: project billing links have an unexpected shape\n"
                )
                return 1
            project_id = project.get("projectId")
            account_name = project.get("billingAccountName")
            enabled = project.get("billingEnabled")
            if (
                not isinstance(project_id, str)
                or not project_id
                or not isinstance(account_name, str)
                or not account_name
                or not isinstance(enabled, bool)
            ):
                sys.stderr.write(
                    "gcloud-billing-json.py: project billing links have an unexpected shape\n"
                )
                return 1
            if project_id in records:
                sys.stderr.write(
                    "gcloud-billing-json.py: one project appeared under multiple billing accounts\n"
                )
                return 1
            record = {
                "projectId": project_id,
                "billingAccountName": account_name,
                "billingEnabled": enabled,
            }
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            separator_bytes = 1 if records else 0
            if (
                len(records) >= MAX_TOTAL_PROJECTS
                or encoded_bytes + separator_bytes + len(encoded) > MAX_OUTPUT_BYTES
            ):
                sys.stderr.write(
                    "gcloud-billing-json.py: total project metadata exceeds the safety limit\n"
                )
                return 1
            records[project_id] = record
            encoded_bytes += separator_bytes + len(encoded)

    ordered = sorted(records.values(), key=lambda record: record["projectId"])
    sys.stdout.write(
        json.dumps(ordered, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
