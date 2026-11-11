"""A copy-in kit for Brain Framework routines, gates, hooks and channels: bounded bf calls, state and emitters.

Copy this file next to your scripts (`routines/_bf.py`, `hooks/_bf.py` or `channels/_bf.py`) and `import _bf` from a
script in the same folder: Python finds it beside the script it runs. It is not an importable `bf` module, and it must
stay standard-library only (Python 3.11+), because brain programs run with the owner's `python3`.

Importing this module opens no connection, reads no environment variable and starts no process.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

# BF's bounds for offline replies, streaming exports, state files, gate events and channel envelopes.
MAX_REPLY = 4 << 20
MAX_PAGES = 100
MAX_STATE = 64 << 20
LINE_BYTES = 64 << 20
ERROR_BYTES = 64 << 10
ENVELOPE_BYTES = 32 << 20
GATE_EVENTS = 64
GATE_EVENT_BYTES = 65_536
EVENT_RESERVE_BYTES = 60_000
KEY_CHARS = 256
REF_CHARS = 4096
TEXT_CHARS = 4096
NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
STALE_CACHE = "identity resolution used a stale cache"
NEEDED_EXPORT_SCOPE = (21, 3)
NEEDED_OBSERVED_SINCE = (21, 4)
_YAML_KEYWORDS = frozenset(
    {"true", "false", "null", "yes", "no", "on", "off", "~", "nan", "inf", ".nan", ".inf", "+.inf", "-.inf"}
)
_YAML_BLOCK_UNSAFE = re.compile(r"(?:^[-?:,]|[:#{}\[\]&*!|>'\"%@`\\]|^[+-]?(?:\d|\.\d))")
_YAML_FLOW_SAFE = re.compile(r"[A-Za-z][A-Za-z0-9._/-]*")
_REMOTE_HOST = r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}"
_REMOTE_URL = re.compile(
    rf"(?:https://({_REMOTE_HOST})/|ssh://git@({_REMOTE_HOST})/|git@({_REMOTE_HOST}):)"
    r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?"
)


class BfError(Exception):
    """A content-free failure safe to write on stderr: never include record bodies, prompts or secrets."""

    def __init__(self, message: str, *, status: int = 1) -> None:
        super().__init__(message)
        self.status = status


def fail(message: str, code: int = 1, *, prefix: str = "") -> NoReturn:
    """Write one content-free diagnostic line on stderr and exit `code`."""
    cleaned = " ".join(CONTROL.sub(" ", message).split())[:500] or "program failed"
    label = f"{prefix}: {cleaned}" if prefix else cleaned
    sys.stderr.write(f"{label}\n")
    raise SystemExit(code)


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def canonical_time(moment: datetime) -> str:
    """UTC timestamp with microsecond precision and `Z`, matching BF's canonical instant spelling."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def window(argv: Sequence[str] | None = None) -> tuple[datetime, datetime]:
    """The half-open window `[start, end)` from `BF_START` and `BF_END`, or `argv` on a manual run."""
    env = (os.environ.get("BF_START", ""), os.environ.get("BF_END", ""))
    values = env if all(env) else tuple(argv or ())
    if len(values) != 2:
        raise BfError("BF_START and BF_END must be set, or pass START and END", status=2)
    start, end = _instant(values[0]), _instant(values[1])
    if start is None or end is None:
        raise BfError("START and END must be ISO 8601 timestamps with timezones", status=2)
    if start >= end:
        raise BfError("START must precede END", status=2)
    return start, end


def state_dir() -> Path:
    """This program's private, machine-local directory from `BF_STATE_DIR`."""
    value = os.environ.get("BF_STATE_DIR", "")
    if not value or not Path(value).is_absolute():
        raise BfError("BF_STATE_DIR must be set to a private absolute folder", status=2)
    path = Path(value)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _state_file(name: str | Path) -> Path:
    if isinstance(name, Path):
        if not name.is_absolute() or not name.name or CONTROL.search(str(name)):
            raise BfError("state file path must be an absolute path without control characters", status=2)
        name.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return name
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or CONTROL.search(name)
    ):
        raise BfError("state file name must be a single filename without path separators", status=2)
    return state_dir() / name


def _no_constant(_value: str) -> NoReturn:
    raise ValueError("non-JSON number")


def load_state(name: str | Path, default: Any = None, *, max_bytes: int = MAX_STATE) -> Any:
    """Read JSON state `name` from `state_dir()`; return a copy of `default` when absent, or fail on damage."""
    path = _state_file(name)
    label = path.name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError:
        raise BfError(f"cannot read {label} in BF_STATE_DIR; delete it to reset state") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise BfError(f"cannot read {label} in BF_STATE_DIR; delete it to reset state")
            data = stream.read(max_bytes + 1)
    except OSError:
        raise BfError(f"cannot read {label} in BF_STATE_DIR; delete it to reset state") from None
    if len(data) > max_bytes:
        raise BfError(f"cannot read {label} in BF_STATE_DIR; delete it to reset state")
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise BfError(f"cannot read {label} in BF_STATE_DIR; delete it to reset state") from None


def save_state(name: str | Path, data: Any, *, max_bytes: int = MAX_STATE) -> None:
    """Atomically write JSON `data` to `name` inside `state_dir()`, keeping any previous file on failure."""
    path = _state_file(name)
    label = path.name
    try:
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError):
        raise BfError(f"state {label} holds a value JSON cannot represent") from None
    if len(payload) > max_bytes:
        raise BfError(f"state {label} exceeds {max_bytes} bytes")
    if path.is_symlink():
        raise BfError(f"cannot write {label} in BF_STATE_DIR; refusing to overwrite a symlink")
    temporary: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{label}.", suffix=".tmp")
        temporary = Path(temp_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise OSError("refusing to replace a symlink")
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
        raise BfError(f"cannot write {label} in BF_STATE_DIR; check that the folder exists and is writable") from None


def _usable_reply(reply: dict[str, Any], *, allow_stale: bool) -> bool:
    problems, stale = reply.get("problems", []), reply.get("stale", [])
    if not allow_stale:
        return not problems and not stale
    return (
        isinstance(problems, list)
        and isinstance(stale, list)
        and all(isinstance(name, str) for name in stale)
        and all(isinstance(problem, dict) and problem.get("error") == STALE_CACHE for problem in problems)
        and not (problems and not stale)
    )


def bf_json(
    *args: str,
    timeout: float = 30,
    allow_stale: bool = False,
    max_bytes: int = MAX_REPLY,
) -> dict[str, Any]:
    """Run `bf *args` with bounded time and output and return its validated JSON object."""
    if timeout <= 0:
        raise BfError("bf call exceeded its time budget")
    try:
        result = subprocess.run(
            ["bf", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BfError("could not run bf within its time budget") from None
    if result.returncode or len(result.stdout) > max_bytes:
        raise BfError("bf command failed or exceeded its output bound")
    try:
        reply = json.loads(result.stdout, parse_constant=_no_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise BfError("bf reply is not valid JSON") from None
    if not isinstance(reply, dict):
        raise BfError("bf reply is not a JSON object")
    if not _usable_reply(reply, allow_stale=allow_stale):
        raise BfError("bf reply is incomplete; run bf status")
    return reply


def bf_pages(
    ref: str = "",
    *,
    rel: str | None = None,
    brain: str | None = None,
    timeout: float = 30,
    deadline: float | None = None,
    allow_stale: bool = False,
    max_pages: int = MAX_PAGES,
    max_bytes: int = MAX_REPLY,
) -> Iterator[dict[str, Any]]:
    """Yield every page of `bf read [ref]` by following `next_offset` within `max_pages` and the time budget."""
    stop_at = time.monotonic() + timeout if deadline is None else deadline
    offset = 0
    for _ in range(max_pages):
        args = [
            "read",
            *([ref] if ref else []),
            *(["--rel", rel] if rel else []),
            *(["--brain", brain] if brain else []),
            *(["--offset", str(offset)] if offset else []),
        ]
        reply = bf_json(*args, timeout=stop_at - time.monotonic(), allow_stale=allow_stale, max_bytes=max_bytes)
        yield reply
        if "next_offset" not in reply:
            return
        following = reply["next_offset"]
        if type(following) is not int or not offset < following < 2**53:
            raise BfError(f"bf read {ref or 'home'} returned an invalid next_offset")
        offset = following
    raise BfError(f"bf read {ref or 'home'} exceeded {max_pages} pages")


def _bf_version(executable: str) -> tuple[int, ...] | None:
    try:
        result = subprocess.run(
            [executable, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.fullmatch(rb"(\d+)\.(\d+)\.(\d+)\s*", result.stdout[:64])
    return tuple(int(part) for part in found.groups()) if found and not result.returncode else None


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        with suppress(OSError):
            child.kill()
    with suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5)
    for pipe in (child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()


def _export_item(line: bytes, label: str, number: int) -> dict[str, Any]:
    try:
        value = json.loads(line, parse_constant=_no_constant)
    except (UnicodeError, ValueError, RecursionError):
        value = None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("brain"), str)
        or not NAME.fullmatch(value["brain"])
        or not isinstance(value.get("ref"), str)
        or not value["ref"]
    ):
        raise BfError(f"bf export{label} printed an invalid line {number}")
    return value


def bf_export(
    scope: str = "",
    *,
    kind: str = "items",
    brain: str | None = None,
    observed_since: datetime | str | None = None,
    timeout: float = 60,
    deadline: float | None = None,
    max_line_bytes: int = LINE_BYTES,
    executable: str | None = None,
) -> Generator[dict[str, Any]]:
    """Stream JSONL objects from `bf export --kind KIND [--scope SCOPE] [--observed-since ...]`, failing closed."""
    bf_bin = executable or shutil.which("bf")
    if bf_bin is None:
        raise BfError("bf is not on PATH; install bf 21.3.0 or later")
    since_arg = ""
    if observed_since is not None:
        if isinstance(observed_since, datetime):
            if observed_since.tzinfo is None:
                raise BfError("observed_since datetime must include a timezone", status=2)
            since_arg = canonical_time(observed_since)
        elif (
            isinstance(observed_since, str)
            and observed_since
            and observed_since.strip() == observed_since
            and not CONTROL.search(observed_since)
        ):
            since_arg = observed_since
        else:
            raise BfError("observed_since must be an ISO 8601 timestamp or period string", status=2)
    stop_at = time.monotonic() + timeout if deadline is None else deadline
    label = f" of {scope}" if scope else ""
    hint_cmd = (
        f"bf export --kind {kind}"
        + (f" --brain {brain}" if brain else "")
        + (f" --scope {scope}" if scope else "")
        + (f" --observed-since {since_arg}" if since_arg else "")
    )
    argv = [
        bf_bin,
        "export",
        "--kind",
        kind,
        *(["--brain", brain] if brain else []),
        *(["--scope", scope] if scope else []),
        *(["--observed-since", since_arg] if since_arg else []),
    ]
    try:
        # Stay in the caller's process group so BF's SIGTERM on timeout or shutdown stops the nested export too.
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        raise BfError("could not start bf; check that it is installed and executable") from None
    pending, errors, number = bytearray(), bytearray(), 0
    try:
        if child.stdout is None or child.stderr is None:
            raise BfError("could not read bf's output")
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, "out")
            selector.register(child.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                remaining = stop_at - time.monotonic()
                if remaining <= 0:
                    raise BfError(f"bf export{label} ran past the {timeout:g}-second limit")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 1 << 16)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "err":
                        errors += chunk[: max(0, ERROR_BYTES - len(errors))]
                    else:
                        begin = 0
                        while (newline := chunk.find(b"\n", begin)) >= 0:
                            pending += chunk[begin:newline]
                            begin = newline + 1
                            number += 1
                            row = _export_item(bytes(pending), label, number)
                            pending.clear()
                            yield row
                        pending += chunk[begin:]
                        if len(pending) > max_line_bytes:
                            raise BfError(
                                f"bf export{label} printed line {number + 1} longer than {max_line_bytes} bytes"
                            )
        try:
            code = child.wait(timeout=max(0.1, stop_at - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise BfError(f"bf export{label} ran past the {timeout:g}-second limit") from None
        if pending:
            number += 1
            yield _export_item(bytes(pending), label, number)
    finally:
        _stop_child(child)
    if code == 2 and (found := _bf_version(bf_bin)) is not None:
        if scope and found[:2] < NEEDED_EXPORT_SCOPE:
            raise BfError(f"bf {'.'.join(map(str, found))} cannot export a scope; install bf 21.3.0 or later")
        if since_arg and found[:2] < NEEDED_OBSERVED_SINCE:
            raise BfError(
                f"bf {'.'.join(map(str, found))} cannot filter by --observed-since; install bf 21.4.0 or later"
            )
    if code:
        raise BfError(f"bf export{label} exited with status {code}; run {hint_cmd} to see why")
    if any(line.startswith(b"bf:") for line in errors.splitlines()) or len(errors) >= ERROR_BYTES:
        raise BfError(f"bf export{label} reported incomplete evidence; run {hint_cmd} to see why")


def visible(text: str) -> str:
    """Escape controls, line and paragraph separators and invisible format characters, which bf replies escape and
    JSON decoding restored."""
    return "".join(
        char.encode("unicode_escape").decode() if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in text
    )


def plain(value: object, limit: int | None = None, *, fallback: str = "") -> str:
    """One inert single-line text fragment: Markdown links reduce to labels, delimiters go and hidden chars show."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", str(value))
    text = visible(re.sub(r"\s+", " ", re.sub(r"[\[\]()<>`]", "", text)).strip())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or fallback


def code(value: object) -> str:
    """A Markdown code span that no backtick inside `value` can close."""
    text = visible(str(value))
    fence = "`" * (max(map(len, re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def git_remote_identity(url: str) -> str:
    """`repo:HOST/OWNER/NAME` for an HTTPS or `git@` SSH remote without credentials; `""` for any other remote."""
    if not isinstance(url, str):
        return ""
    match = _REMOTE_URL.fullmatch(url.strip())
    if not match:
        return ""
    host = next(group for group in match.groups()[:3] if group)
    return f"repo:{host}/{match[4]}".lower()


def _event_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1


def _normalize_event(raw: Mapping[str, Any], *, hash_prefix: str = "event") -> dict[str, str]:
    key = str(raw.get("key", "")).strip()
    if not key:
        raise BfError("every gate event requires a nonempty key")
    key = visible(key)
    if len(key) > KEY_CHARS:
        safe_prefix = re.sub(r"[^A-Za-z0-9._:-]+", "-", hash_prefix).strip("-")[:64] or "event"
        digest = hashlib.sha256(str(raw["key"]).encode("utf-8")).hexdigest()
        key = f"{safe_prefix}:sha256:{digest}"
    raw_text = " ".join(str(raw.get("text", "")).split()) or "event"
    text = visible(raw_text)
    if len(text) > TEXT_CHARS:
        text = text[: TEXT_CHARS - 1] + "…"
    event: dict[str, str] = {"key": key, "text": text}
    ref = raw.get("ref")
    if isinstance(ref, str) and ref and len(ref) <= REF_CHARS and not CONTROL.search(ref):
        event["ref"] = ref
    return event


def emit_events(
    events: Iterable[Mapping[str, Any]],
    *,
    overflow: Callable[[list[Mapping[str, Any]]], Mapping[str, Any]] | None = None,
    overflow_key: str = "events:more",
    overflow_ref: str = "",
) -> list[dict[str, str]]:
    """Print `{"events": [...]}` within BF's 64-event / 64 KiB gate budget, summarizing any overflow."""
    materialized = list(events)
    normalized = [_normalize_event(item, hash_prefix=str(item.get("group") or "event")) for item in materialized]
    # All 64 events fit directly when there is no 65th event and the encoded payload stays within 64 KiB.
    if len(normalized) <= GATE_EVENTS:
        rendered = json.dumps({"events": normalized}, ensure_ascii=False)
        if len(rendered.encode("utf-8")) <= GATE_EVENT_BYTES:
            sys.stdout.write(rendered + "\n")
            sys.stdout.flush()
            return normalized
    kept: list[dict[str, str]] = []
    kept_raw: list[Mapping[str, Any]] = []
    dropped: list[Mapping[str, Any]] = []
    used = 2
    for raw_item, candidate in zip(materialized, normalized, strict=True):
        cost = _event_size(candidate)
        if len(kept) < GATE_EVENTS - 1 and used + cost <= EVENT_RESERVE_BYTES:
            kept.append(candidate)
            kept_raw.append(raw_item)
            used += cost
        else:
            dropped.append(raw_item)

    def _make_summary() -> dict[str, str]:
        if overflow is not None:
            return _normalize_event(overflow(dropped))
        return _normalize_event(
            {
                "key": overflow_key,
                "text": f"{len(dropped)} more events are not listed.",
                **({"ref": overflow_ref} if overflow_ref else {}),
            }
        )

    if dropped:
        summary = _make_summary()
        while (
            kept
            and len(json.dumps({"events": [*kept, summary]}, ensure_ascii=False).encode("utf-8")) > GATE_EVENT_BYTES
        ):
            kept.pop()
            dropped.insert(0, kept_raw.pop())
            summary = _make_summary()
        kept.append(summary)
    sys.stdout.write(json.dumps({"events": kept}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return kept


def _yaml_scalar(value: Any, *, key: str = "", in_flow: bool = False) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        try:
            return json.dumps(value, allow_nan=False)
        except ValueError:
            raise BfError("action frontmatter holds a non-JSON number") from None
    if not isinstance(value, str):
        raise BfError("unsupported action frontmatter value")
    text = " ".join(CONTROL.sub(" ", value).split())
    if not in_flow and key in {"date", "updated"} and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if in_flow:
        if _YAML_FLOW_SAFE.fullmatch(text) and text.lower() not in _YAML_KEYWORDS:
            return text
        return json.dumps(text, ensure_ascii=False)
    if not text or text != text.strip() or text.lower() in _YAML_KEYWORDS or _YAML_BLOCK_UNSAFE.search(text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _yaml_value(value: Any, *, key: str = "", in_flow: bool = False) -> str:
    if isinstance(value, list):
        items = ", ".join(_yaml_value(item, in_flow=True) for item in value)
        return f"[{items}]"
    if isinstance(value, Mapping):
        pairs: list[str] = []
        for sub_key, sub_val in value.items():
            if not isinstance(sub_key, str) or not NAME.fullmatch(sub_key.replace("_", "-")):
                raise BfError("invalid action frontmatter key")
            pairs.append(f"{sub_key}: {_yaml_value(sub_val, key=sub_key, in_flow=True)}")
        return "{" + ", ".join(pairs) + "}"
    return _yaml_scalar(value, key=key, in_flow=in_flow)


def format_action(frontmatter: Mapping[str, Any], body: str | Iterable[str]) -> str:
    """Render an OKF `type: action` Markdown document from `frontmatter` and `body`."""
    meta = {"type": "action", "status": "draft", **dict(frontmatter)}
    if meta.get("type") != "action":
        raise BfError("action frontmatter type must be 'action'")
    if meta.get("status") not in {"draft", "stable", "deprecated"}:
        raise BfError("action status must be draft, stable or deprecated")
    lines = ["---"]
    for key, value in meta.items():
        if not isinstance(key, str) or not NAME.fullmatch(key.replace("_", "-")):
            raise BfError("invalid action frontmatter key")
        lines.append(f"{key}: {_yaml_value(value, key=key)}")
    lines.extend(["---", ""])
    content = body if isinstance(body, str) else "\n".join(body)
    return "\n".join(lines) + content.rstrip("\n") + "\n"


def emit_action(frontmatter: Mapping[str, Any], body: str | Iterable[str]) -> None:
    """Write a complete OKF `ACTION.md` note to stdout for a `routine` with `output: action`."""
    sys.stdout.write(format_action(frontmatter, body))
    sys.stdout.flush()


def read_envelope(*, max_bytes: int = ENVELOPE_BYTES) -> dict[str, Any]:
    """Read and validate one outgoing channel message envelope from stdin (`id`, `text`, optional `conversation`)."""
    data = sys.stdin.buffer.read(max_bytes + 1)
    try:
        message = json.loads(data, parse_constant=_no_constant) if len(data) <= max_bytes else None
    except (UnicodeError, ValueError, RecursionError):
        message = None
    if (
        not isinstance(message, dict)
        or not all(isinstance(message.get(key), str) and message[key] for key in ("id", "text"))
        or not isinstance(message.get("conversation", ""), str)
    ):
        raise BfError("expected one outgoing message with a nonempty id and text on stdin", status=2)
    return message


def emit_receipt(
    id: str,
    *,
    conversation: str = "",
) -> dict[str, str]:
    """Print a channel delivery receipt (`{"id": ..., "conversation": ...}`) to stdout."""
    if not isinstance(id, str) or not id.strip() or CONTROL.search(id):
        raise BfError("receipt id must be a nonempty single-line string")
    if not isinstance(conversation, str) or CONTROL.search(conversation):
        raise BfError("receipt conversation must be a single-line string")
    receipt: dict[str, str] = {"id": id, **({"conversation": conversation} if conversation else {})}
    sys.stdout.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return receipt
