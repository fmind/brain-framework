"""A copy-in kit for Brain Framework sensors: bounded provider calls and records BF accepts.

Copy this file next to your sensors (`sensors/_sensor.py`) and `import _sensor` from a sensor in the same folder:
Python finds it beside the script it runs. It is not an importable `bf` module, and it must stay standard-library
only (Python 3.11+), because sensors run with the owner's `python3`. Written for Brain Framework 20.1 and later,
record format `version: 8`; review it like any sensor code, since it belongs to your brain once copied.

The kit enforces the record bounds in https://fmind.github.io/brain-framework/docs/limits/. A sensor built on it
prints one JSON array of records, or fails closed: exit 1, nothing on stdout and one content-free line on stderr,
so BF keeps the records it saved. Importing it opens no connection and starts no process.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import selectors
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

# BF's record bounds, as `docs/limits.md` lists them.
MAX_ID, MAX_ENCODED_ID, MAX_TITLE, MAX_TEXT = 4096, 7988, 4096, 4 << 20
MAX_REF, MAX_LINKS = 8192, 1000  # One URL, link, alias or field value; links, aliases and mapped values per record.
MAX_FIELDS = 2 << 20  # Every record field other than `text`, as UTF-8 JSON.
MAX_OUTPUT = 16 << 20  # One sensor run's JSON array; BF's `max_bytes` defaults to 64 MiB.
# C0 and C1 control characters, which BF rejects in ids, titles, URLs and identities.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A surrogate code point left alone: `json.loads` already joins valid pairs, and UTF-8 cannot encode one.
SURROGATE = re.compile(r"[\ud800-\udfff]")
# Unicode format characters (category Cf), such as U+200B, which BF rejects in identities: two would look alike.
INVISIBLE = re.compile(
    r"[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064"
    r"\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f]"
)
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")
# Variables that would make a provider or `git` read another repository or load code: a Git hook's GIT_DIR, for one.
LOCAL = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
LOCAL += ("GIT_ALTERNATE_OBJECT_DIRECTORIES",)


class SensorError(Exception):
    """A content-free failure, safe to print on stderr: never put provider output or secrets in its message."""


def fail(message: str) -> NoReturn:
    """Print one content-free stderr line, nothing on stdout, and exit 1 so BF keeps its saved records."""
    print(" ".join(CONTROL.sub(" ", message).split())[:500] or "collection failed", file=sys.stderr)
    raise SystemExit(1)


def main(collect: Callable[[], Iterable[Mapping[str, Any]]], failure: str) -> None:
    """Emit what `collect` returns, or fail closed: with a SensorError's message, else with `failure`.

    An unexpected error names only its type, such as `KeyError`: its message could quote provider data.
    """
    try:
        emit(collect())
    except SensorError as error:
        fail(str(error))
    except Exception as error:  # noqa: BLE001 - any failure must exit 1 without a traceback quoting provider data
        fail(f"{failure} ({type(error).__name__})")


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return parsed


def window(argv: Sequence[str] | None = None) -> tuple[datetime, datetime]:
    """The half-open window [start, end) as aware datetimes.

    BF sets `BF_START` and `BF_END` (ISO 8601 UTC) for every sensor run, and they take precedence. Outside BF,
    such as a manual test run, pass `[START, END]`, such as `sys.argv[1:3]`.
    """
    env = (os.environ.get("BF_START", ""), os.environ.get("BF_END", ""))
    values = env if all(env) else tuple(argv or ())
    if len(values) != 2:
        raise SensorError("no window: run through bf, or pass START and END")
    try:
        start, end = _instant(values[0]), _instant(values[1])
    except ValueError:
        raise SensorError("START and END must be ISO 8601 timestamps with timezones") from None
    if start >= end:
        raise SensorError("START must precede END")
    return start, end


def state_dir() -> Path:
    """This program's private, machine-local directory from `BF_STATE_DIR`, for cursors kept between runs.

    BF creates it with mode 0700, outside the brain. It may be deleted at any time: treat an empty one as a first
    run, and keep only data you can rebuild there, never evidence.
    """
    value = os.environ.get("BF_STATE_DIR", "")
    if not value or not Path(value).is_absolute():
        raise SensorError("BF_STATE_DIR is unset: run through bf, or set it to a private absolute folder")
    path = Path(value)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def run(argv: Sequence[str], *, timeout: float, max_bytes: int) -> bytes:
    """Run a provider CLI with literal argv, no shell, from `/`, and return its bounded stdout.

    Its stderr is discarded, never echoed: it can quote private data. It starts from `/` because some CLIs, such as
    gws, load a `.env` file from their working directory or any parent, and one in the brain could switch accounts.
    The child stays in BF's process group, so BF's cancellation kills it too; the kit kills it on timeout or
    overflow, and BF ends any descendant when the sensor exits.
    """
    name = Path(argv[0]).name
    env = {key: value for key, value in os.environ.items() if key not in LOCAL}
    try:
        child = subprocess.Popen(
            list(argv), cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        raise SensorError(f"cannot start {name}; check that it is installed on PATH") from None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if child.stdout is None:
            raise SensorError(f"{name} has no output pipe")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise SensorError(f"{name} exceeded its {timeout:g}s timeout")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        raise SensorError(f"{name} output exceeds {max_bytes} bytes")
        if code := child.wait():
            raise SensorError(f"{name} failed with status {code}; check its authentication and arguments")
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


class _SameHostHttps(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to HTTPS on the same host and port: a token sent in headers must not leak."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or (new.hostname, new.port) != (old.hostname, old.port) or new.username:
            raise SensorError("the provider redirected outside its HTTPS host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, timeout: float, max_bytes: int, headers: Mapping[str, str] | None = None) -> bytes:
    """GET an HTTPS URL with verified TLS and return its bounded body; refuses other schemes and credentials in URLs.

    Redirects stay on the same HTTPS host. Pass tokens in `headers`, loaded at run time from the environment, never
    from a file in the brain. `timeout` bounds each socket operation and the whole body read.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or CONTROL.search(url):
        raise SensorError("fetch needs an HTTPS URL without credentials")
    host = parts.hostname
    opener = urllib.request.build_opener(
        _SameHostHttps(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")  # noqa: S310 - HTTPS checked
    deadline = time.monotonic() + timeout
    try:
        with opener.open(request, timeout=timeout) as response:
            body = bytearray()
            # read1 returns after one socket read, so a trickling body cannot outlast the deadline.
            while chunk := response.read1(65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise SensorError(f"{host} answered more than {max_bytes} bytes")
                if time.monotonic() >= deadline:
                    raise SensorError(f"{host} exceeded the {timeout:g}s timeout")
            return bytes(body)
    except urllib.error.HTTPError as error:
        error.close()
        raise SensorError(f"{host} answered HTTP {error.code}; check the token and URL") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        raise SensorError(f"cannot reach {host}; check the network and URL") from None


def fetch_json(url: str, *, timeout: float, max_bytes: int, headers: Mapping[str, str] | None = None) -> Any:
    """`fetch` an HTTPS URL and decode its JSON body; invalid JSON fails without quoting it."""
    body = fetch(url, timeout=timeout, max_bytes=max_bytes, headers={"Accept": "application/json", **(headers or {})})
    try:
        return json.loads(body)
    except (UnicodeError, ValueError):
        raise SensorError(f"{urlsplit(url).hostname} answered invalid JSON") from None


def _identity(value: str) -> str:
    """`value` when BF accepts it as an identity, else "": one bad provider value never fails a whole collection."""
    ok = (
        len(value) <= MAX_REF
        and IDENTITY.fullmatch(value)
        and not CONTROL.search(value)
        and not SURROGATE.search(value)
    )
    return value if ok and not INVISIBLE.search(value) else ""


def person(email: str) -> str:
    """`person:email/ADDRESS`, lowercased and percent-encoded as the reviewed examples write it; "" when invalid."""
    address = email.strip().lower()
    if address.count("@") != 1 or CONTROL.search(address):
        return ""
    return _identity("person:email/" + quote(address, safe="/:@+").replace("~", "%7E"))


def account(host: str, login: str) -> str:
    """`person:HOST/LOGIN` for a provider account, lowercased, such as `person:github.com/octocat`; "" when invalid.

    Declare it as an alias on the person's note, beside their `person:email/` identity, to join both.
    """
    parts = [part.strip().lower() for part in (host, login)]
    if not all(parts) or any(CONTROL.search(part) for part in parts):
        return ""
    return _identity("person:" + "/".join(quote(part, safe="") for part in parts))


def repo(host: str, owner: str, name: str) -> str:
    """`repo:HOST/OWNER/NAME`, lowercased, such as `repo:github.com/fmind/brain-framework`; "" when invalid."""
    parts = [part.strip().lower() for part in (host, owner, name)]
    if not all(parts) or any(CONTROL.search(part) for part in parts):
        return ""
    return _identity("repo:" + "/".join(quote(part, safe="") for part in parts))


def _identities(values: Iterable[object], name: str) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise SensorError(f"{name} must be a list of identities")
    kept = [value for value in values if isinstance(value, str) and _identity(value)]
    return list(dict.fromkeys(kept))[:MAX_LINKS]  # The caller's order sets which values a cut keeps.


def _text(value: Any) -> Any:
    """`value` with each lone UTF-16 surrogate, such as a JSON `\\ud83d` cut from its pair, replaced by U+FFFD."""
    if isinstance(value, str):
        return SURROGATE.sub("\ufffd", value)
    if isinstance(value, Mapping):
        return {_text(key): _text(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value]
    return value


def _timestamp(value: object) -> str:
    """An aware time as canonical UTC, as BF stores it, or "" for a naive, invalid or out-of-range one."""
    try:
        moment = value if isinstance(value, datetime) else _instant(value) if isinstance(value, str) else None
        if moment is None or moment.tzinfo is None:
            return ""
        return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return ""


def record(
    id: str,  # noqa: A002 - BF's own field name
    title: str,
    *,
    text: str = "",
    time: str | datetime = "",
    url: str = "",
    links: Iterable[str] = (),
    aliases: Iterable[str] = (),
    attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One record BF accepts: invalid optional values are normalized or dropped, never a whole collection's failure.

    The id must already be valid, since it is the record's identity. Titles become one bounded line, text is cut to
    BF's bound with `attributes.partial`, lone surrogates in the title, text and attributes become U+FFFD, invalid
    times, URLs, links and aliases are dropped, links and aliases and each `*_refs` attribute keep their first 1,000
    valid identities, and an invalid `updated`, `observed`, `kind` or `status` is removed.
    Never pass `fields`: BF computes them from your mappings.
    """
    if (
        not isinstance(id, str)
        or not id.strip()
        or len(id) > MAX_ID
        or CONTROL.search(id)
        or SURROGATE.search(id)
        or len(quote(id, safe="/@:")) > MAX_ENCODED_ID
    ):
        raise SensorError("a record id is empty, too long or holds control characters or a lone surrogate")
    extra = _text(dict(attributes or {}))
    for key in [key for key in extra if key.endswith("_refs")]:
        extra[key] = _identities(extra[key], f"attributes.{key}")
    for key in [key for key in ("updated", "observed") if key in extra]:
        if instant := _timestamp(extra[key]):
            extra[key] = instant
        else:
            del extra[key]
    # The standard vocabulary maps `kind` and `status` as single strings: another value would fail the collection.
    for key in [key for key in ("kind", "status") if key in extra]:
        value = extra[key]
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_REF or CONTROL.search(value):
            del extra[key]
    if "partial" in extra and not isinstance(extra["partial"], bool):
        raise SensorError("attributes.partial must be a boolean")
    text = _text(text)
    if len(text) > MAX_TEXT:
        text, extra["partial"] = text[:MAX_TEXT], True
    valid = isinstance(url, str) and len(url) <= MAX_REF and not CONTROL.search(url) and not SURROGATE.search(url)
    parts = urlsplit(url) if valid else None
    item = {
        "id": id,
        "title": " ".join(CONTROL.sub(" ", str(_text(title))).split())[:MAX_TITLE].rstrip() or "(untitled)",
        "text": text,
        "time": _timestamp(time),
        "url": url if parts and parts.scheme in ("http", "https") and parts.hostname else "",
        "links": sorted(_identities(links, "links")),
        "aliases": sorted(_identities(aliases, "aliases")),
        "attributes": extra,
    }
    others = {key: value for key, value in item.items() if key != "text"}
    if len(json.dumps(others, ensure_ascii=False).encode("utf-8", "surrogatepass")) > MAX_FIELDS:
        raise SensorError("a record's fields other than text exceed 2 MiB; keep bulky content in text")
    return item


def emit(records: Iterable[Mapping[str, Any]], *, max_bytes: int = MAX_OUTPUT) -> None:
    """Print every record as ONE JSON array on stdout, or fail before printing anything.

    Refuses duplicate ids, which would hide one item, and `fields`, which BF computes itself.
    """
    items, seen = [], set()
    for item in records:
        identifier = item.get("id")
        if identifier in seen:
            raise SensorError("two records share one id")
        if "fields" in item:
            raise SensorError("a sensor never prints fields; BF computes them from bf.yaml mappings")
        seen.add(identifier)
        items.append(item)
    try:
        payload = json.dumps(items, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError):
        raise SensorError("a record holds a value JSON cannot represent") from None
    if len(payload) > max_bytes:
        raise SensorError(f"records exceed {max_bytes} bytes; narrow the window")
    sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.flush()


# Best effort: these shapes catch common leaks, not every secret. Each quantifier is bounded, so a long or adversarial
# provider text costs time proportional to its length.
_KEY = r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)"
_PAIR = r"""(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[a-z0-9_.-]{0,40})"""
_PAIR += r"""(["']?\s{0,8}[:=]\s{0,8})"""
_SECRETS = (
    (re.compile(_KEY, re.DOTALL), "[REDACTED KEY]"),
    (re.compile(r"(?i)\b(bearer|token|basic)(\s{1,8})[A-Za-z0-9._~+/=-]{16,4096}"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{20,255})"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,255}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}"), "[REDACTED]"),
    (re.compile(_PAIR + r"(\"[^\"\n]{0,4096}\"|'[^'\n]{0,4096}'|[^\s,;&\"']{1,4096})"), r"\1\2[REDACTED]"),
)


def redact(text: str) -> str:
    """Mask common credential shapes in provider text before it enters a record, such as tokens and private keys.

    Best effort: it catches bearer and basic tokens, GitHub, AWS, Slack and Google API keys, JWTs, private key
    blocks and `password=`/`token:`-style pairs, never every secret.
    """
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text
