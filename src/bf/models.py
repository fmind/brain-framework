"""Shared constants, errors, strict JSON encoding, Unicode/terminal sanitization, secret redaction, and UTC helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Collection
from contextlib import suppress
from datetime import UTC, date, datetime
from typing import Any, Literal, overload

FORMAT = 19
DEFAULT_NOTES = ("concepts", "projects", "actions")
NOTES_FILE = "memories/_notes.jsonl"
RESERVED_SOURCES = frozenset(
    {
        "bf",
        "memories",
        "notes",
        "record",
        "logs",
        "schemas",
        "evals",
        "sensors",
        "routines",
        "agents",
        "clients",
        "prompts",
        "http",
        "https",
        "mailto",
        "urn",
    }
)
MAX_FILE = 256 << 20
MAX_BATCH = 16 << 20
MAX_NOTE = 4 << 20
MAX_SCHEMA = 1 << 20
MAX_FILES = 100_000
MAX_TOTAL_FILES = 1_000_000
SCALE_WARN_RECORDS = 20_000
SCALE_WARN_BYTES = 64 << 20
NAME = r"^[a-z][a-z0-9-]{0,63}$"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}\Z")
EVENT_KIND_PAT = r"^[a-z][a-z0-9._-]{0,127}$"
EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}\Z")
PLACEHOLDERS = frozenset(("brain", "loop", "state", "start", "end", "trigger", "run"))
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]*)\s*\}\}")
ANY_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
DURATION_PAT = r"^(?:[0-9]+(?:s|m|h|d|w|y))+$"
DURATION_ZERO_PAT = r"^(?:0|(?:[0-9]+(?:s|m|h|d|w|y))+)$"
DURATION_RE = re.compile(r"^(?:[0-9]+(?:s|m|h|d|w|y))+\Z")
_DURATION_PART_RE = re.compile(r"([0-9]+)(s|m|h|d|w|y)")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31_557_600}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ASCII_TEXT_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_SEPS = frozenset(("\u2028", "\u2029"))

_PEM_TAG = r"[A-Z0-9 ]{0,40}(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----"
PEM_BEG_RE = re.compile(rf"-----BEGIN {_PEM_TAG}")
PEM_END_RE = re.compile(rf"-----END {_PEM_TAG}")
_PEM_BODY = rf"(?:(?!-----(?:BEGIN|END) {_PEM_TAG})[\s\S]){{0,65536}}"


class PemFilter:
    """Stateful multi-line PEM private-key suppressor used by `proc.drain_tail`."""

    __slots__ = ("in_pem", "max_bytes", "pem_bytes")

    def __init__(self, max_bytes: int = 16384) -> None:
        self.in_pem = False
        self.pem_bytes = 0
        self.max_bytes = max_bytes

    def filter_line(self, text: str, nbytes: int, *, oversized: bool = False) -> Literal["pass", "drop", "redact"]:
        """Classify one line: `'drop'` (suppress), `'redact'` (emit `[REDACTED]`), or `'pass'` (normal processing)."""
        begs = [m.start() for m in PEM_BEG_RE.finditer(text)]
        ends = [m.start() for m in PEM_END_RE.finditer(text)]
        last_beg, last_end = (begs[-1] if begs else -1), (ends[-1] if ends else -1)
        if self.in_pem:
            self.pem_bytes += nbytes
            if last_end > last_beg:
                self.in_pem, self.pem_bytes = False, 0
                return "drop"
            if last_beg > last_end:
                self.pem_bytes = 0
                return "drop"
            if oversized or self.pem_bytes <= self.max_bytes:
                return "drop"
            self.in_pem, self.pem_bytes = False, 0
        if begs:
            if last_beg > last_end:
                self.in_pem, self.pem_bytes = True, 0
            return "redact"
        return "pass"

    def scan_dropped(self, text: str) -> bool:
        """Track PEM markers across discarded chunks of an oversized line; return True when entering a new PEM block."""
        begs = [m.start() for m in PEM_BEG_RE.finditer(text)]
        ends = [m.start() for m in PEM_END_RE.finditer(text)]
        if not begs and not ends:
            return False
        last_beg, last_end = (begs[-1] if begs else -1), (ends[-1] if ends else -1)
        if last_beg > last_end:
            entered = not self.in_pem
            self.in_pem, self.pem_bytes = True, 0
            return entered
        if last_end > last_beg:
            self.in_pem, self.pem_bytes = False, 0
        return False


_KV_KEY = (
    r"(?i:(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{1,128}(?:\\?[\"'])?\s*[:=])"
    r"(?:(?:[A-Za-z0-9_-]{0,64}?(?:password|passwd|passphrase|secret|private[_-]?key|api[_-]?key"
    r"|access[_-]?key|credential[s]?|authorization|cookie)(?:[_-][A-Za-z0-9_-]{1,64}+)?)"
    r"|(?:(?:[A-Za-z0-9_-]{1,64}?[_-])?(?:token|jwt)))(?![A-Za-z0-9_-]))"
)
_KV_VAL = r'(?:\\?"(?:[^"\\\r\n]|\\.){1,8192}"?|\\?\'(?:[^\'\\\r\n]|\\.){1,8192}\'?|[^\s"\',;}{\]]{1,8192})'
_SECRET_RE = re.compile(
    r"(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:proxy[_-]?)?authorization(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?[A-Za-z0-9._-]+\s+[A-Za-z0-9._~+/=,-]{8,}(?:\\?[\"'])?"
    r"|\b(?:set-)?cookie(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?[^\r\n\"']{6,2048}(?:\\?[\"'])?)"
    r"|(?<=://)[^/\s:@]{0,128}:[^/\s@]{1,4096}(?=@)"
    r"|(?:gh[pousr]_|github_pat_|glpat-|AKIA|ASIA|AIza|ya29\.|sk-|sk_live_|sk_test_|rk_live_|whsec_|xox[baprs]-|npm_|pypi-|hf_)[A-Za-z0-9._-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    rf"|-----BEGIN {_PEM_TAG}{_PEM_BODY}(?:-----END {_PEM_TAG})?"
    rf"|{_KV_KEY}(?:\\?[\"'])?\s*[:=]\s*{_KV_VAL}"
)
_KV_KEY_RE = re.compile(
    r"(?i)(?:password|passwd|passphrase|secret|private[_-]?key|api[_-]?key|access[_-]?key|credential|authorization|cookie|token|jwt)"
)


def is_invisible_char(ch: str) -> bool:
    """Return True for Unicode format controls (`Cf`), Variation Selectors, and Tags (`U+E0000..U+E01EF`)."""
    code = ord(ch)
    if code < 0x80:
        return False
    return unicodedata.category(ch) == "Cf" or 0xFE00 <= code <= 0xFE0F or 0xE0000 <= code <= 0xE01EF


def invisible(value: str) -> bool:
    """Return True if `value` contains any invisible Unicode format, tag, or variation-selector character."""
    return not value.isascii() and any(is_invisible_char(ch) for ch in value)


def _escape_char(ch: str) -> str:
    units = ch.encode("utf-16-be").hex()
    return "".join(f"\\u{units[i : i + 4]}" for i in range(0, len(units), 4))


def printable(text: str) -> str:
    """Escape C0/C1 controls, Unicode line/paragraph separators, and invisible format/tag chars for safe terminals."""
    if text.isascii() and not _CONTROL.search(text):
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F or ch in _LINE_SEPS or is_invisible_char(ch):
            out.append(_escape_char(ch))
        else:
            out.append(ch)
    return "".join(out)


def strip_unsafe_text(value: str) -> str:
    """Strip C0/C1 terminal controls (keeping `\\t`, `\\n`) and invisible format/tag chars; normalize `\\r`/Zl/Zp."""
    if value.isascii() and "\r" not in value and _ASCII_TEXT_CTRL.search(value) is None:
        return value
    norm = value.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in norm:
        code = ord(ch)
        if ch in _LINE_SEPS:
            out.append("\n")
        elif ch in {"\t", "\n"}:
            out.append(ch)
        elif code < 0x20 or 0x7F <= code <= 0x9F or is_invisible_char(ch):
            continue
        else:
            out.append(ch)
    return "".join(out)


MAX_JSON_DEPTH = 64


def _check_json_depth(val: object, label: str) -> None:
    stack: list[tuple[object, int]] = [(val, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            if depth >= MAX_JSON_DEPTH:
                raise Error(f"{label}: JSON nesting exceeds {MAX_JSON_DEPTH} levels")
            stack.extend((v, depth + 1) for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            if depth >= MAX_JSON_DEPTH:
                raise Error(f"{label}: JSON nesting exceeds {MAX_JSON_DEPTH} levels")
            stack.extend((v, depth + 1) for v in item if isinstance(v, (dict, list)))


@overload
def sanitize_record_strings(val: dict[str, Any]) -> dict[str, Any]: ...
@overload
def sanitize_record_strings(val: object) -> object: ...
def sanitize_record_strings(val: object) -> object:
    """Recursively strip terminal escapes and invisible format/tag chars from JSON string values; reject unsafe dict keys."""

    def _walk(item: object, depth: int = 0) -> object:
        if depth > MAX_JSON_DEPTH:
            raise Error(f"record: JSON nesting exceeds {MAX_JSON_DEPTH} levels")
        if isinstance(item, str):
            return strip_unsafe_text(item)
        if isinstance(item, list):
            return [_walk(sub, depth + 1) for sub in item]
        if isinstance(item, dict):
            out: dict[Any, object] = {}
            for k, v in item.items():
                if isinstance(k, str):
                    clean_k = strip_unsafe_text(k)
                    if clean_k != k or clean_k in out:
                        raise Error("record contains unsafe control or invisible characters in object key")
                    out[clean_k] = _walk(v, depth + 1)
                else:
                    out[k] = _walk(v, depth + 1)
            return out
        return item

    return _walk(val)


def redact_secrets(text: str, *, secrets: Collection[str] = ()) -> str:
    """Scrub exact secret literals (`len >= 8`) first, then common tokens, private keys, JWTs, and URL credentials."""
    out = text
    if secrets:
        for secret in sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True):
            if secret in out:
                out = out.replace(secret, "[REDACTED]")
    return _SECRET_RE.sub("[REDACTED]", out)


def redact_truncated_prefix(text: str, keep_chars: int, *, secrets: Collection[str] = ()) -> str:
    """Redact `text` and drop its trailing `keep_chars` safety margin in original-character coordinates."""
    cut = len(text) - keep_chars
    if cut <= 0:
        return ""
    spans: list[tuple[int, int]] = [m.span() for m in _SECRET_RE.finditer(text)]
    if secrets:
        for secret in sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True):
            pos = 0
            while (idx := text.find(secret, pos)) != -1:
                spans.append((idx, idx + len(secret)))
                pos = idx + len(secret)
    if not spans:
        return text[:cut].rstrip()
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if s >= cut:
            break
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out: list[str] = []
    cur = 0
    for s, e in merged:
        if s > cur:
            out.append(text[cur:s])
        out.append("[REDACTED]")
        cur = min(e, cut)
    if cur < cut:
        out.append(text[cur:cut])
    return "".join(out).rstrip()


def redact_value(val: object, *, secrets: Collection[str] = ()) -> object:
    """Recursively redact secrets in strings and mask any non-empty value under a secret-named dict key."""
    secret_tuple = tuple(sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True)) if secrets else ()

    def _walk(item: object, depth: int = 0) -> object:
        if depth > MAX_JSON_DEPTH:
            raise Error(f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")
        if isinstance(item, str):
            return redact_secrets(item, secrets=secret_tuple)
        if isinstance(item, list):
            return [_walk(x, depth + 1) for x in item]
        if isinstance(item, dict):
            out: dict[object, object] = {}
            for key, sub in item.items():
                redacted_key = redact_secrets(key, secrets=secret_tuple) if isinstance(key, str) else key
                is_secret = isinstance(key, str) and bool(_KV_KEY_RE.search(key))
                if is_secret and sub is not None and sub != "" and sub != [] and sub != {}:
                    out[redacted_key] = "[REDACTED]"
                else:
                    out[redacted_key] = _walk(sub, depth + 1)
            return out
        return item

    return _walk(val)


class Error(Exception):
    """A safe, actionable diagnostic message without untrusted payload values."""


class BusyError(Error):
    """Another process holds the brain writer or loop lock."""


class InputError(Error):
    """Invalid CLI arguments or input syntax (exit code 2)."""


class FormatError(Error):
    """A configuration file declares a format version this release does not read."""


FAILURES = (Error, OSError, UnicodeError)


def check_version(value: dict[str, object], name: str) -> None:
    """Require `version: FORMAT` before validating fields."""
    if (found := value.get("version")) == FORMAT and type(found) is int:
        return
    ver = f"version {found}" if type(found) is int and 0 <= found < 1000 else "an invalid version"
    why = "has no version" if "version" not in value else f"declares {ver}"
    raise FormatError(f"{name} {why}; this release reads version: {FORMAT}")


def parse_duration(
    value: object, label: str = "duration", *, allow_zero: bool = False, maximum: int = 315_576_000
) -> int:
    """Convert an explicit duration string (`30s`, `15m`, `1h30m`, `1d`, `1w`, `1y`; or `0`/`0s`) into whole seconds."""
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        raise Error(f"{label} must not contain newline characters")
    if allow_zero and (
        (type(value) is int and value == 0) or (isinstance(value, str) and value.strip() in {"0", "0s"})
    ):
        return 0
    if not isinstance(value, str) or len(stripped := value.strip()) > 32 or not DURATION_RE.fullmatch(stripped):
        hint = "0, 30s, 15m, 1h30m, 1d, or 1w" if allow_zero else "30s, 15m, 1h30m, 1d, or 1w"
        raise Error(f"{label} must be an explicit duration string such as {hint}")
    total = sum(int(num) * _DURATION_UNITS[unit] for num, unit in _DURATION_PART_RE.findall(stripped))
    if (not allow_zero and total < 1) or total > maximum:
        raise Error(f"{label} must be between {0 if allow_zero else 1}s and {maximum}s")
    return total


def clean(value: str, label: str = "value") -> str:
    if not value.strip() or _CONTROL.search(value):
        raise Error(f"{label} must be nonempty and contain no control characters")
    return value


def clean_env(value: str, label: str = "env") -> str:
    if _CONTROL.search(value):
        raise Error(f"{label} must contain no control characters")
    return value


def clean_text(value: str, label: str = "text") -> str:
    if _TEXT_CONTROL.search(value):
        raise Error(f"{label} contains forbidden control characters")
    return value


def encode(value: object) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode()
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise Error("JSON values must be finite UTF-8 data without lone surrogates") from error


def terminal(value: object) -> bytes:
    text = encode(value).decode("utf-8")
    if text.isascii() and not _CONTROL.search(text[:-1]):
        return text.encode("utf-8")
    # Escape C1 controls, line separators, and invisible format/tag chars while keeping trailing newline.
    body = text[:-1]
    out: list[str] = []
    for ch in body:
        code = ord(ch)
        if 0x7F <= code <= 0x9F or ch in _LINE_SEPS or is_invisible_char(ch):
            out.append(_escape_char(ch))
        else:
            out.append(ch)
    return ("".join(out) + "\n").encode("utf-8")


def decode(data: bytes | str, label: str = "JSON", *, check_duplicates: bool = True) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        if len(out := dict(items)) != len(items):
            raise Error(f"{label} contains a duplicate key")
        return out

    def no_nan(_: str) -> None:
        raise Error(f"{label} numbers must be finite")

    try:
        parsed = json.loads(data, object_pairs_hook=pairs if check_duplicates else None, parse_constant=no_nan)
    except Error:
        raise
    except (ValueError, RecursionError, UnicodeError) as error:
        raise Error(f"{label}: invalid JSON") from error
    _check_json_depth(parsed, label)
    return parsed


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_date(value: str, label: str = "date") -> str:
    if "\n" in value or "\r" in value:
        raise Error(f"{label} requires YYYY-MM-DD")
    try:
        if date.fromisoformat(value).isoformat() != value or value in {"0001-01-01", "9999-12-31"}:
            raise ValueError
    except ValueError:
        raise Error(f"{label} requires YYYY-MM-DD") from None
    return value


def normalize_time(value: str, label: str = "timestamp") -> str:
    if "\n" in value or "\r" in value:
        raise Error(f"{label} requires an ISO 8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise Error(f"{label} requires an ISO 8601 timestamp with timezone") from error
    if parsed.tzinfo is None:
        raise Error(f"{label} requires a timezone offset or Z")
    try:
        return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as error:
        raise Error(f"{label} is outside the supported UTC range") from error


def normalize_iso_date_or_time(value: str, label: str = "date") -> str:
    """Validate and normalize either `YYYY-MM-DD` or an ISO 8601 timestamp with timezone."""
    return normalize_date(value, label) if len(value) == 10 else normalize_time(value, label)


def validate_stale_after(value: object, label: str = "stale_after") -> str:
    """Validate and normalize `stale_after` as either an explicit duration (`<=32` chars) or ISO date/timestamp."""
    if not isinstance(value, str) or not value:
        raise Error(f"{label} must be an explicit duration (e.g. 90d) or ISO date")
    if len(value) <= 32 and "\n" not in value and "\r" not in value and DURATION_RE.fullmatch(value):
        parse_duration(value, label)
        return value
    return normalize_iso_date_or_time(value, label)


def ts_key(raw: str, *, start_of_day: bool = False) -> str:
    """Return a sortable UTC ISO key from a date or timestamp (date-only defaults to end-of-day UTC)."""
    if not raw:
        return ""
    suffix = "T00:00:00.000000+00:00" if start_of_day else "T23:59:59.999999+00:00"
    with suppress(ValueError, OverflowError):
        dt = datetime.fromisoformat(f"{raw}{suffix}" if len(raw) == 10 else raw)
        if dt.tzinfo is not None:
            return dt.astimezone(UTC).isoformat(timespec="microseconds")
    return raw


def now_utc(now: datetime | None = None) -> str:
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def log_line(message: str, *, severity: str = "INFO", time_iso: str = "", **fields: object) -> bytes:
    """Render one structured UTF-8 JSON log line (`time`, `severity`, `message`)."""
    payload = {"time": time_iso or now_utc(), "severity": severity.upper(), "message": message}
    return terminal({**payload, **{key: val for key, val in fields.items() if val is not None}})


def silence_broken_pipe() -> None:
    """Redirect stdout fd to `/dev/null` after a downstream pipe closes so Python exit stays clean."""
    with suppress(OSError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
        finally:
            os.close(devnull)


def failure(error: BaseException) -> str:
    """Format an unexpected error without exposing raw payloads."""
    if isinstance(error, Error):
        return str(error)
    if isinstance(error, UnicodeError):
        return (
            "a file is not valid UTF-8; run bf check to locate it"
            if isinstance(error, UnicodeDecodeError)
            else "arguments and terminal locale must use UTF-8"
        )
    if isinstance(error, OSError):
        extra = f" ({error.strerror})" if error.strerror else ""
        return f"inaccessible file or directory{extra}; check permissions and path"
    return "unexpected failure"
