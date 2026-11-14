#!/usr/bin/env python3
"""Capture the Drive folders of My Drive and shared-with-me items, asserting parent `part-of` relations, through gws.

Shared drives are outside this catalog (`corpora: user`). Each folder sets `@type` to `"Folder"` and links its
parents directly in `relations["part-of"]`.
"""

from __future__ import annotations

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
from datetime import UTC, datetime
from urllib.parse import quote

MAX_BYTES = 16 << 20
MAX_FOLDERS = 10000
MAX_PAGES = 20
MAX_ID = 4096
MAX_ENCODED_ID = 7988
MAX_REF = 8192
MAX_LINKS = 1000
FOLDER = "application/vnd.google-apps.folder"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")


class InvalidError(ValueError):
    """Provider output violated the expected schema or bounds."""


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


def iso_utc(value: datetime | str) -> str:
    if isinstance(value, str):
        if not value:
            return ""
        dt = datetime.fromisoformat(value)
    else:
        dt = value
    if dt.tzinfo is None:
        raise InvalidError("timestamps require timezones")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
    item_type: str = "",
    text: str = "",
    time_value: str = "",
    url: str = "",
    aliases: Sequence[str] = (),
    relations: dict[str, list[str]] | None = None,
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    out: dict[str, object] = {"id": identifier, "title": line(title, identifier)}
    if item_type:
        out["@type"] = item_type
    if text:
        out["text"] = text
    if time_value:
        out["time"] = iso_utc(time_value)
    if valid_url := reference(url):
        out["url"] = valid_url
    valid_aliases = sorted({a for a in aliases if reference(a)})
    if valid_aliases:
        out["aliases"] = valid_aliases
    if relations:
        cleaned = {k: list(dict.fromkeys(r for r in v if reference(r)))[:MAX_LINKS] for k, v in relations.items()}
        nonempty = {k: v for k, v in cleaned.items() if v}
        if nonempty:
            out["relations"] = nonempty
    if attributes:
        out["attributes"] = attributes
    return out


def collect() -> list[dict[str, object]]:
    # https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
    token = ""
    tokens: set[str] = set()
    seen: set[str] = set()
    records = []
    for _ in range(MAX_PAGES):
        params: dict[str, object] = {
            "q": f"trashed=false and mimeType='{FOLDER}'",
            "corpora": "user",
            "pageSize": 1000,
            "fields": (
                "kind,nextPageToken,incompleteSearch,files(id,name,mimeType,parents,createdTime,modifiedTime,webViewLink)"
            ),
        }
        if token:
            params["pageToken"] = token
        data = run(["gws", "drive", "files", "list", "--params", json.dumps(params)], timeout=120, max_bytes=MAX_BYTES)
        page = json.loads(data)
        if (
            not isinstance(page, dict)
            or page.get("kind") != "drive#fileList"
            or page.get("incompleteSearch", False) is not False
            or not isinstance(page.get("files", []), list)
        ):
            raise InvalidError("gws returned an incomplete or invalid folder listing")
        for folder in page.get("files", []):
            if not isinstance(folder, dict):
                raise InvalidError("gws returned an invalid folder")
            identifier, name, parents = folder.get("id"), folder.get("name"), folder.get("parents", [])
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in seen
                or CONTROL.search(identifier)
                or len(identifier) > MAX_ID
                or len(quote(identifier, safe="/@:")) > MAX_ENCODED_ID
                or not isinstance(name, str)
                or not name.strip()
                or folder.get("mimeType") != FOLDER
                or not isinstance(parents, list)
                or any(not isinstance(p, str) or not p for p in parents)
            ):
                raise InvalidError("gws returned an invalid, duplicate or over-long folder identity")
            seen.add(identifier)
            if len(seen) > MAX_FOLDERS:
                raise InvalidError(f"the catalog has more than {MAX_FOLDERS} folders")
            created = optional_instant(folder.get("createdTime"))
            modified = optional_instant(folder.get("modifiedTime"))
            parent_refs = [ref for parent in parents if (ref := reference("drive:" + quote(parent, safe="")))]
            alias = reference("drive:" + quote(identifier, safe=""))
            records.append(
                make_record(
                    identifier,
                    line(name, "Untitled Drive folder"),
                    item_type="Folder",
                    text="Drive folder: " + name,
                    # The folder's creation is its event; modification is the revision BF compares.
                    time_value=created or modified,
                    url=str(folder.get("webViewLink", "")),
                    aliases=[alias] if alias else (),
                    relations={"part-of": parent_refs},
                    # Selected fields only: `updated` is BF's reserved provider-modification time.
                    attributes={
                        "name": name,
                        "parents": parents,
                        **({"created": created} if created else {}),
                        **({"updated": modified} if modified else {}),
                    },
                )
            )
        token = page.get("nextPageToken", "")
        if not isinstance(token, str):
            raise InvalidError("gws returned an invalid continuation token")
        if not token:
            return records
        if token in tokens:
            raise InvalidError("gws repeated a continuation token")
        tokens.add(token)
    raise InvalidError(f"the catalog needs more than {MAX_PAGES} pages")


if __name__ == "__main__":
    cli(
        lambda: emit(collect(), max_bytes=MAX_BYTES),
        "Drive folder collection failed",
        "check gws access and folder listing completeness",
    )
