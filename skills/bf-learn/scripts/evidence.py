#!/usr/bin/env python3
"""Read one exact bf reply whole, capture it, or compare a retained capture with a new read.

`read` runs the offline `bf read` and assembles JSON chunks; `capture` and `compare` use stdin only.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import TypeAlias, cast

# Agents run this helper with whatever python3 is on PATH: keep it compatible with Python 3.11.
JSON: TypeAlias = "bool | int | float | str | list[JSON] | dict[str, JSON] | None"

MAX_REPLY = 4 << 20  # UTF-8 bytes of one assembled exact read; larger evidence needs a section.
MAX_INPUT = 9 << 20  # Two bounded bf replies, plus the small capture envelope.


def object_value(value: JSON) -> dict[str, JSON]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def encode(value: JSON) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def unique(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    result: dict[str, JSON] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def identity(reply: dict[str, JSON]) -> tuple[str, str]:
    brain, ref = reply.get("brain"), reply.get("ref")
    if not isinstance(brain, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", brain):
        raise ValueError("expected a named brain")
    if not isinstance(ref, str) or not ref or len(ref) > 8192 or any(ord(c) < 32 for c in ref):
        raise ValueError("expected an exact ref")
    return brain, ref


def body(reply: dict[str, JSON]) -> dict[str, JSON]:
    """Keep the answer, never backlinks, aliases resolved elsewhere, or a page listing."""
    if "chunk" in reply or "next_offset" in reply:
        raise ValueError("assemble the complete exact read before retaining evidence")
    if "page" in reply or ("text" in reply) == ("record" in reply):
        raise ValueError("expected an exact note, section or record read")
    if "text" in reply:
        if not isinstance(reply["text"], str):
            raise ValueError("expected note text")
        return {"text": reply["text"]}
    record = object_value(reply["record"])
    if not isinstance(record.get("id"), str) or not isinstance(record.get("title"), str):
        raise ValueError("expected a record with id and title")
    if "attributes" in record:
        object_value(record["attributes"])
    return {"record": record}


def exact(ref: str, brain: str) -> dict[str, JSON]:
    """Follow next_offset through an exact read's JSON chunks and verify their digest before parsing."""
    pieces: list[str] = []
    offset, total, digest = 0, 0, ""
    while True:
        # bf reads offline and never runs sensors. Its diagnostics may contain private paths: discard them.
        result = subprocess.run(  # noqa: S603 - literal argv; main() rejects a ref that could start an option
            ["bf", "read", ref, "--brain", brain, *(["--offset", str(offset)] if offset else [])],  # noqa: S607
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode or len(result.stdout) > MAX_REPLY:
            raise ValueError("read failed or exceeded the reply limit")
        reply = object_value(json.loads(result.stdout, object_pairs_hook=unique))
        if "chunk" not in reply:
            if offset:
                raise ValueError("chunks ended early")
            return reply
        chunk = reply["chunk"]
        if not isinstance(chunk, str) or not chunk or reply.get("format") != "json" or reply.get("offset") != offset:
            raise ValueError("unexpected chunk")
        if offset == 0:
            total, digest = cast("int", reply.get("total_characters")), cast("str", reply.get("sha256"))
            if type(total) is not int or total > MAX_REPLY or not isinstance(digest, str):
                raise ValueError("unexpected chunk")
        elif (reply.get("total_characters"), reply.get("sha256")) != (total, digest):
            raise ValueError("the reply changed while reading")
        pieces.append(chunk)
        offset += len(chunk)
        if "next_offset" not in reply:
            break
        if reply["next_offset"] != offset or offset >= total:
            raise ValueError("unexpected continuation")
    data = "".join(pieces).encode()
    if offset != total or len(data) > MAX_REPLY or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("the reply changed while reading")
    return object_value(json.loads(data, object_pairs_hook=unique))


def fingerprint(value: dict[str, JSON]) -> str:
    # Re-observation alone changes no evidence; retain it in the capture but ignore it in comparisons.
    if "record" in value:
        record = dict(object_value(value["record"]))
        attributes = dict(object_value(record.get("attributes", {})))
        attributes.pop("observed", None)
        if attributes:
            record["attributes"] = attributes
        else:
            record.pop("attributes", None)
        value = {"record": record}
    return hashlib.sha256(encode(value)).hexdigest()


def limitations(reply: dict[str, JSON]) -> list[JSON]:
    result: list[JSON] = []
    if reply.get("problems") or reply.get("stale"):
        result.append("incomplete read")
    if "record" in reply:
        attributes = object_value(object_value(reply["record"]).get("attributes", {}))
        if attributes.get("partial"):
            result.append("partial record")
        collection = object_value(reply.get("collection", {}))
        if collection.get("failed") or collection.get("freshness") != "fresh" or collection.get("state") != "active":
            result.append("source freshness is not established")
    return result


def capture(reply: dict[str, JSON]) -> dict[str, JSON]:
    brain, ref = identity(reply)
    value = body(reply)
    if reply.get("problems") or reply.get("stale"):
        raise ValueError("incomplete read; inspect bf status before capturing")
    return {
        "capture_version": 1,
        # The same canonical UTC form as bf reply instants, so they compare as strings.
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "brain": brain,
        "ref": ref,
        **value,
        "sha256": fingerprint(value),
        "collection": reply.get("collection", {}),
        "limitations": limitations(reply),
    }


def compare(before: dict[str, JSON], after: dict[str, JSON]) -> dict[str, JSON]:
    if type(before.get("capture_version")) is not int or before["capture_version"] != 1:
        raise ValueError("expected an evidence capture version 1")
    captured_at = before.get("captured_at")
    if not isinstance(captured_at, str) or datetime.fromisoformat(captured_at).tzinfo is None:
        raise ValueError("expected a capture time with timezone")
    brain, ref = identity(before)
    if (brain, ref) != identity(after):
        raise ValueError("cannot compare different brains or refs")
    previous, current = body(before), body(after)
    if before.get("sha256") != fingerprint(previous):
        raise ValueError("capture content does not match its digest")
    changed = fingerprint(previous) != fingerprint(current)
    # Old source freshness does not decide whether a new read is current. A partial baseline does.
    reasons = limitations(after)
    if "record" in previous and object_value(object_value(previous["record"]).get("attributes", {})).get("partial"):
        reasons.append("partial baseline")
    return {
        "brain": brain,
        "ref": ref,
        "state": "unknown" if reasons else "changed" if changed else "unchanged",
        "content_changed": changed,
        "limitations": reasons,
        "baseline_at": captured_at,
    }


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["read"] and len(arguments) == 4 and arguments[2] == "--brain":
        ref, brain = arguments[1], arguments[3]
        # bf would parse a leading hyphen as an option; the brain is always an option value.
        if not ref or ref.startswith("-") or len(ref) > 8192 or any(ord(c) < 32 for c in ref) or not brain:
            sys.stderr.write("evidence: expected an exact ref and a brain name or path\n")
            return 2
        try:
            output = encode(exact(ref, brain))
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, RecursionError):
            sys.stderr.write("evidence: bf read failed or changed while reading; inspect the exact read and retry\n")
            return 1
        sys.stdout.buffer.write(output + b"\n")
        return 0
    if len(arguments) != 1 or arguments[0] not in {"capture", "compare"}:
        sys.stderr.write("usage: evidence.py read REF --brain BRAIN | evidence.py capture|compare < JSON\n")
        return 2
    try:
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT:
            raise ValueError("input exceeds 9 MiB")
        remaining = data.decode().strip()
        decoder = json.JSONDecoder(object_pairs_hook=unique)
        values: list[dict[str, JSON]] = []
        count = 1 if sys.argv[1] == "capture" else 2
        for _ in range(count):
            value, end = decoder.raw_decode(remaining)
            values.append(object_value(cast("JSON", value)))
            remaining = remaining[end:].strip()
        if remaining:
            raise ValueError("unexpected trailing input")
        result = capture(values[0]) if count == 1 else compare(values[0], values[1])
        output = encode(result)
    except (ValueError, TypeError, RecursionError):
        # Parser errors may quote private data. Do not copy them into logs or stdout.
        sys.stderr.write("evidence: invalid or incomplete input; supply exact bf reads and an intact capture\n")
        return 1
    sys.stdout.buffer.write(output + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
