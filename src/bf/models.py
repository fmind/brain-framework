"""Shared constants, errors, strict JSON encoding, text sanitization, and UTC timestamps."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime

FORMAT = 16
EVAL_FORMAT = 1
AUTHORED = ("projects", "concepts", "actions")
_RES_EXTRA = ("memories", "notes", "record", "logs", "evals", "bf", "loops")
RESERVED_SOURCES = frozenset((*AUTHORED, *_RES_EXTRA))
MAX_FILE = 64 << 20
MAX_BATCH = 16 << 20
MAX_NOTE = 4 << 20
MAX_SCHEMA = 1 << 20
MAX_FILES = 100_000
MAX_TOTAL_FILES = 1_000_000
NAME = r"^[a-z][a-z0-9-]{0,63}$"
NAME_RE = re.compile(NAME)
EVENT_KIND_PAT = r"^[a-z][a-z0-9._-]{0,127}$"
EVENT_KIND_RE = re.compile(EVENT_KIND_PAT)
DURATION_PAT = r"^(?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d|w|y))+$"
DURATION_ZERO_PAT = r"^(?:0|(?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d|w|y))+)$"
DURATION_RE = re.compile(DURATION_PAT)
_DURATION_PART_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d|w|y)")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0, "y": 31_557_600.0}

_FORMAT = (
    r"\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f"
    r"\ufeff\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    r"\U000e0001\U000e0020-\U000e007f"
)
_INVISIBLE = re.compile(f"[{_FORMAT}]")
_SEPARATORS = r"\u2028\u2029"
_TERMINAL = re.compile(rf"[\x7f-\x9f{_SEPARATORS}{_FORMAT}]")
_UNPRINTABLE = re.compile(rf"[\x00-\x1f\x7f-\x9f{_SEPARATORS}{_FORMAT}]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PEM_TAG = r"[A-Z0-9 ]{0,32}PRIVATE KEY-----"
_PEM_BODY = rf"(?:(?!-----(?:BEGIN|END) {_PEM_TAG})[\s\S]){{0,16384}}"
_KV_KEY = (
    r"(?i:(?<![A-Za-z0-9])(?:password|passwd|(?:client[_-]?)?secret"
    r"(?:[_-]?access[_-]?key|[_-]?key)?|private[_-]?key|api[_-]?key"
    r"|(?:access|refresh|auth|api|session|id|bot|private|gh|github|oauth|bearer)[_-]?token))"
)
_KV_VAL = r'(?:\\?"[^"\r\n]{1,8192}"?|\\?\'[^\'\r\n]{1,8192}\'?|[^\s"\',;}{\]]{1,8192})'
_SECRET_RE = re.compile(
    r"(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|\bauthorization\s*[:=]\s*basic\s+[A-Za-z0-9+/=-]{8,})"
    r"|(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,128}(?=@)"
    r"|(?:gh[pousr]_|github_pat_|AKIA|ASIA|AIza|ya29\.|sk-|xox[baprs]-)[A-Za-z0-9._-]{8,}"
    rf"|-----BEGIN {_PEM_TAG}{_PEM_BODY}(?:-----END {_PEM_TAG})?|{_KV_KEY}(?:\\?[\"'])?\s*[:=]\s*{_KV_VAL}"
)


_KV_KEY_RE = re.compile(rf"(?:{_KV_KEY}|(?i:(?<![A-Za-z0-9])(?:proxy[_-]?)?authorization))$")


def redact_secrets(text: str) -> str:
    """Scrub common bearer/API tokens, private keys, and URL credentials before persisting diagnostics."""
    return _SECRET_RE.sub("[REDACTED]", text)


def redact_value(val: object) -> object:
    """Recursively redact secrets in strings and values under secret-named dict keys."""
    if isinstance(val, str):
        return redact_secrets(val)
    if isinstance(val, list):
        return [redact_value(x) for x in val]
    if isinstance(val, dict):
        out: dict[object, object] = {}
        for k, v in val.items():
            rk, sec = redact_secrets(k) if isinstance(k, str) else k, isinstance(k, str) and bool(_KV_KEY_RE.search(k))
            if sec and isinstance(v, str) and v:
                out[rk] = "[REDACTED]"
            elif sec and isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                out[rk] = ["[REDACTED]" for _ in v]
            else:
                out[rk] = redact_value(v)
        return out
    return val


class Error(Exception):
    """A safe, actionable diagnostic message without untrusted payload values."""


class BusyError(Error):
    """Another process holds the brain writer lock."""


class InputError(Error):
    """Invalid CLI arguments or input syntax (exit code 2)."""


class FormatError(Error):
    """A configuration file declares a format version this release does not read."""


FAILURES = (Error, OSError, UnicodeError)


def check_version(value: dict[str, object], name: str, *, expected: int = FORMAT) -> None:
    """Require `version: expected` before validating fields."""
    if (found := value.get("version")) == expected and type(found) is int:
        return
    ver = f"version {found}" if type(found) is int and 0 <= found < 1000 else "an invalid version"
    why = "has no version" if "version" not in value else f"declares {ver}"
    raise FormatError(f"{name} {why}; this release reads version: {expected}")


def parse_duration_seconds(
    value: object, label: str = "duration", *, allow_zero: bool = False, maximum: float = 3_155_760_000.0
) -> float:
    """Convert numeric seconds or humanized duration strings (`300ms`, `15m`, `1h30m`, `1d`, `1w`) into seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and allow_zero and value.strip() == "0":
        seconds = 0.0
    elif isinstance(value, str) and DURATION_RE.fullmatch(stripped := value.strip()):
        seconds = sum(float(num) * _DURATION_UNITS[unit] for num, unit in _DURATION_PART_RE.findall(stripped))
    else:
        raise Error(f"{label} must be a duration such as 30s, 15m, 1h30m, 1d, or 1w")
    if not math.isfinite(seconds):
        raise Error(f"{label} must be finite")
    if seconds < 0 or (seconds == 0 and not allow_zero) or seconds > maximum:
        raise Error(f"{label} must be >{'=' if allow_zero else ''} 0s and <= {maximum:g}s")
    return seconds


def parse_duration(
    value: object, label: str = "duration", *, allow_zero: bool = False, maximum: int = 315_576_000
) -> int:
    """Convert integer seconds or humanized duration strings into whole seconds within bounds."""
    sec = parse_duration_seconds(value, label, allow_zero=allow_zero)
    whole = round(sec)
    if abs(sec - whole) > 1e-9 or (not allow_zero and whole < 1) or whole > maximum:
        raise Error(f"{label} must be whole seconds between {0 if allow_zero else 1}s and {maximum}s")
    return whole


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


def invisible(value: str) -> bool:
    return not value.isascii() and _INVISIBLE.search(value) is not None


def _escape(match: re.Match[str]) -> str:
    units = match[0].encode("utf-16-be").hex()
    return "".join(f"\\u{units[i : i + 4]}" for i in range(0, len(units), 4))


def printable(text: str) -> str:
    return _UNPRINTABLE.sub(_escape, text)


def encode(value: object) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode()
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise Error("JSON values must be finite UTF-8 data without lone surrogates") from error


def terminal(value: object) -> bytes:
    text = encode(value).decode("utf-8")
    return (_TERMINAL.sub(_escape, text) if _TERMINAL.search(text) else text).encode()


def decode(data: bytes | str, label: str = "JSON", *, check_duplicates: bool = True) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        if len(out := dict(items)) != len(items):
            raise Error(f"{label} contains a duplicate key")
        return out

    def no_nan(_: str) -> None:
        raise Error(f"{label} numbers must be finite")

    try:
        return json.loads(data, object_pairs_hook=pairs if check_duplicates else None, parse_constant=no_nan)
    except Error:
        raise
    except (ValueError, RecursionError, UnicodeError) as error:
        raise Error(f"{label}: invalid JSON") from error


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_date(value: str, label: str = "date") -> str:
    try:
        if date.fromisoformat(value).isoformat() != value or value in {"0001-01-01", "9999-12-31"}:
            raise ValueError
    except ValueError:
        raise Error(f"{label} requires YYYY-MM-DD") from None
    return value


def normalize_time(value: str, label: str = "timestamp") -> str:
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


def now_utc(now: datetime | None = None) -> str:
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def log_line(message: str, *, severity: str = "INFO", time_iso: str = "", **fields: object) -> bytes:
    """Render one structured UTF-8 JSON log line (`time`, `severity`, `message`)."""
    payload = {"time": time_iso or now_utc(), "severity": severity.upper(), "message": message}
    return terminal({**payload, **{k: v for k, v in fields.items() if v is not None}})


def failure(error: BaseException) -> str:
    """Format an unexpected error without exposing raw payloads."""
    if isinstance(error, Error):
        return str(error)
    if isinstance(error, UnicodeError):
        return (
            "a file is not valid UTF-8; run bf check --validate to locate it"
            if isinstance(error, UnicodeDecodeError)
            else "arguments and terminal locale must use UTF-8"
        )
    if isinstance(error, OSError):
        extra = f" ({error.strerror})" if error.strerror else ""
        return f"inaccessible file or directory{extra}; check permissions and path"
    return "unexpected failure"
