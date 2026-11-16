"""Shared constants, errors, strict JSON encoding, text sanitization, and UTC timestamps."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime

from durationpy import DurationError
from durationpy import from_str as _duration_from_str

FORMAT = 12
AUTHORED = ("projects", "concepts", "actions")
RESERVED_SOURCES = frozenset({"projects", "concepts", "actions", "memories", "notes", "record", "logs", "evals", "bf"})
MAX_FILE = 64 << 20
MAX_BATCH = 16 << 20
MAX_NOTE = 4 << 20
MAX_SCHEMA = 1 << 20
MAX_FILES = 100_000
NAME = r"^[a-z][a-z0-9-]{0,63}$"
NAME_RE = re.compile(NAME)
DURATION_PAT = r"^(?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d|w|y))+$"
DURATION_ZERO_PAT = r"^(?:0|(?:[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d|w|y))+)$"
DURATION_RE = re.compile(DURATION_PAT)

_FORMAT = (
    r"\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064"
    r"\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f"
)
_INVISIBLE = re.compile(f"[{_FORMAT}]")
_SEPARATORS = r"\u2028\u2029"
_TERMINAL = re.compile(rf"[\x7f-\x9f{_SEPARATORS}{_FORMAT}]")
_UNPRINTABLE = re.compile(rf"[\x00-\x1f\x7f-\x9f{_SEPARATORS}{_FORMAT}]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class Error(Exception):
    """A safe, actionable diagnostic message without untrusted payload values."""


class BusyError(Error):
    """Another process holds the brain writer lock."""


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


def parse_duration_seconds(
    value: object, label: str = "duration", *, allow_zero: bool = False, maximum: float = 3_155_760_000.0
) -> float:
    """Convert numeric seconds or humanized duration strings (`300ms`, `15m`, `1h30m`, `1d`, `1w`) into seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if not math.isfinite(seconds):
            raise Error(f"{label} must be finite")
    elif isinstance(value, str):
        stripped = value.strip()
        if allow_zero and stripped == "0":
            seconds = 0.0
        elif DURATION_RE.fullmatch(stripped):
            try:
                seconds = _duration_from_str(stripped).total_seconds()
            except DurationError, OverflowError, ValueError:
                raise Error(f"{label} is not a valid duration") from None
        else:
            raise Error(f"{label} must be a duration such as 30s, 15m, 1h30m, 1d, or 1w")
    else:
        raise Error(f"{label} must be a duration such as 30s, 15m, 1h30m, 1d, or 1w")
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
    """Require a nonempty single-line string without control characters."""
    if not value.strip() or _CONTROL.search(value):
        raise Error(f"{label} must be nonempty and contain no control characters")
    return value


def clean_env(value: str, label: str = "env") -> str:
    """Require a single-line string (possibly empty) without C0/DEL/C1 control characters."""
    if _CONTROL.search(value):
        raise Error(f"{label} must contain no control characters")
    return value


def clean_text(value: str, label: str = "text") -> str:
    """Require text without C0/DEL/C1 control characters other than tab, CR, and LF."""
    if _TEXT_CONTROL.search(value):
        raise Error(f"{label} contains forbidden control characters")
    return value


def invisible(value: str) -> bool:
    """Whether `value` contains invisible Unicode format or bidirectional control characters."""
    return not value.isascii() and _INVISIBLE.search(value) is not None


def _escape(match: re.Match[str]) -> str:
    units = match[0].encode("utf-16-be").hex()
    return "".join(f"\\u{units[i : i + 4]}" for i in range(0, len(units), 4))


def printable(text: str) -> str:
    """Escape control, separator, and format characters for single-line stderr diagnostics."""
    return _UNPRINTABLE.sub(_escape, text)


def encode(value: object) -> bytes:
    """Serialize `value` to compact UTF-8 JSON ending in a newline."""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode()
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise Error("JSON values must be finite UTF-8 data without lone surrogates") from error


def terminal(value: object) -> bytes:
    """Serialize `value` to compact UTF-8 JSON while escaping terminal/invisible control characters."""
    text = encode(value).decode("utf-8")
    return (_TERMINAL.sub(_escape, text) if _TERMINAL.search(text) else text).encode()


def decode(data: bytes | str, label: str = "JSON") -> object:
    """Decode one JSON value rejecting duplicate object keys, NaN, and Infinity."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        if len(out := dict(items)) != len(items):
            raise Error(f"{label} contains a duplicate key")
        return out

    def no_nan(_: str) -> None:
        raise Error(f"{label} numbers must be finite")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=no_nan)
    except Error:
        raise
    except (ValueError, RecursionError, UnicodeError) as error:
        raise Error(f"{label}: invalid JSON") from error


def digest(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def normalize_date(value: str, label: str = "date") -> str:
    """Validate strict `YYYY-MM-DD` calendar dates."""
    try:
        if date.fromisoformat(value).isoformat() != value or value in {"0001-01-01", "9999-12-31"}:
            raise ValueError
    except ValueError:
        raise Error(f"{label} requires YYYY-MM-DD") from None
    return value


def normalize_time(value: str, label: str = "timestamp") -> str:
    """Validate a timezone-aware ISO 8601 timestamp and normalize to UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise Error(f"{label} requires an ISO 8601 timestamp with timezone") from error
    if parsed.tzinfo is None:
        raise Error(f"{label} requires a timezone offset or Z")
    try:
        utc = parsed.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise Error(f"{label} is outside the supported UTC range") from error
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def now_utc(now: datetime | None = None) -> str:
    """Return `now` (or the current UTC instant) formatted as `YYYY-MM-DDTHH:MM:SSZ`."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def failure(error: BaseException) -> str:
    """Format an unexpected error without exposing raw payloads."""
    if isinstance(error, Error):
        return str(error)
    if isinstance(error, UnicodeDecodeError):
        return "a file is not valid UTF-8; run bf validate to locate it"
    if isinstance(error, UnicodeError):
        return "arguments and terminal locale must use UTF-8"
    if isinstance(error, OSError):
        extra = f" ({error.strerror})" if error.strerror else ""
        return f"inaccessible file or directory{extra}; check permissions and path"
    return "unexpected failure"
