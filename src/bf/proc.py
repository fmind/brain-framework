"""Isolated subprocess execution, least-privilege environment allowlisting, fd-safe stderr draining, and process-group teardown."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import unquote, urlsplit

from bf import config, models
from bf.models import MAX_BATCH, Error, encode
from bf.storage import Store, private_dir, relative


class StopFlag(Protocol):
    """Lock-free or event-based cancellation flag (`threading.Event` or signal-safe boolean flag)."""

    def is_set(self) -> bool: ...
    def set(self) -> None: ...


_S_XDG = ("CONFIG", "DATA", "CACHE", "STATE")
_PROXY_ENV_KEYS = frozenset(p for k in ("HTTP", "HTTPS", "NO", "ALL") for p in (f"{k}_PROXY", f"{k.lower()}_proxy"))
# Minimal non-credential POSIX & TLS runtime allowlist (no SSH_AUTH_SOCK, GH_TOKEN, GOOGLE_APPLICATION_CREDENTIALS, DBUS, DISPLAY).
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "SHELL",
    "TERM",
    "TZ",
    "NO_COLOR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    *(f"XDG_{k}_HOME" for k in _S_XDG),
    *(p for k in ("HTTP", "HTTPS", "NO", "ALL") for p in (f"{k}_PROXY", f"{k.lower()}_proxy")),
)
type _LineHook = Callable[[bytes], str | None]


def drain_tail(
    stream: IO[bytes],
    limit: int,
    stop: threading.Event | None = None,
    *,
    on_line: _LineHook | None = None,
    secrets: Iterable[str] = (),
) -> tuple[threading.Thread, Callable[[], str]]:
    """Drain a duplicated OS fd of `stream`, redacting lines incrementally before retaining the last `limit` bytes.

    Duplicating the fd eliminates fd-reuse races when `Popen.__exit__` closes the parent pipe after join timeouts,
    and redacting before buffer eviction eliminates boundary-truncation secret leaks.
    """
    dup_fd: int | None = None
    with suppress(OSError, ValueError):
        dup_fd = os.dup(stream.fileno())
    buf = bytearray()
    buf_lock = threading.Lock()
    secret_tuple = tuple(sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True))

    def _append_redacted(raw_line: bytes) -> None:
        if not raw_line.strip():
            return
        safe_text: str | None = None
        if on_line is not None:
            with suppress(*models.FAILURES):
                safe_text = on_line(raw_line)
        if safe_text is None:
            safe_text = models.redact_secrets(raw_line.decode("utf-8", "replace").strip(), secrets=secret_tuple)
        if not safe_text:
            return
        chunk = (safe_text + "\n").encode("utf-8", "replace")
        with buf_lock:
            buf.extend(chunk)
            if len(buf) > limit * 2:
                del buf[:-limit]

    keep_chars = max(8192, max((len(s) for s in secret_tuple), default=0))
    pem = models.PemFilter()

    def _emit_normal(raw_line: bytes) -> None:
        action = pem.filter_line(raw_line.decode("utf-8", "replace"), len(raw_line))
        if action == "redact":
            _append_redacted(b"[REDACTED]")
        elif action == "pass":
            _append_redacted(raw_line)

    def _emit_oversized(raw_prefix: bytes) -> None:
        end = len(raw_prefix)
        for back in range(1, min(4, end) + 1):
            b = raw_prefix[end - back]
            if (b & 0xC0) != 0x80:
                expected = 1 if b < 0x80 else (2 if b < 0xE0 else (3 if b < 0xF0 else 4))
                if back < expected:
                    end -= back
                break
        raw_text = raw_prefix[:end].decode("utf-8", "replace")
        action = pem.filter_line(raw_text, len(raw_prefix), oversized=True)
        if action == "drop":
            return
        if action == "redact":
            _append_redacted(b"[REDACTED]")
            return
        prefix = models.redact_truncated_prefix(raw_text, keep_chars, secrets=secret_tuple)
        _append_redacted((f"{prefix} [truncated]" if prefix else "[truncated]").encode("utf-8"))

    def _scan_dropped(raw_chunk: bytes) -> None:
        if pem.scan_dropped(raw_chunk.decode("utf-8", "replace")):
            _append_redacted(b"[REDACTED]")

    def _read() -> None:
        pending = bytearray()
        dropping = False
        drop_tail = b""
        poller: select.poll | None = None
        if dup_fd is not None:
            with suppress(OSError, ValueError):
                poller = select.poll()
                poller.register(dup_fd, select.POLLIN)
        try:
            while True:
                done = bool(stop and stop.is_set())
                if poller is not None and not poller.poll(0 if done else 200):
                    if done:
                        break
                    continue
                chunk = os.read(dup_fd, 8192) if dup_fd is not None else stream.read(8192)
                if not chunk:
                    break
                pending.extend(chunk)
                if dropping:
                    nl = pending.find(b"\n")
                    if nl == -1:
                        scan_bytes = drop_tail + bytes(pending)
                        _scan_dropped(scan_bytes)
                        drop_tail = scan_bytes[-160:]
                        pending.clear()
                        continue
                    _scan_dropped(drop_tail + bytes(pending[:nl]))
                    drop_tail = b""
                    del pending[: nl + 1]
                    dropping = False
                while (nl := pending.find(b"\n")) != -1:
                    if nl >= 65536:
                        _emit_oversized(bytes(pending[:65536]))
                        _scan_dropped(bytes(pending[65536 - 160 : nl]))
                    else:
                        _emit_normal(bytes(pending[:nl]))
                    del pending[: nl + 1]
                if len(pending) >= 65536:
                    # Redact 64 KiB prefix in original-char coordinates and track PEM markers across discarded bytes.
                    _emit_oversized(bytes(pending[:65536]))
                    drop_tail = bytes(pending[65536 - 160 :])
                    _scan_dropped(drop_tail)
                    drop_tail = drop_tail[-160:]
                    pending.clear()
                    dropping = True
            if pending and not dropping:
                _emit_normal(bytes(pending))
                pending.clear()
            with buf_lock:
                del buf[: max(0, len(buf) - limit)]
        except (OSError, ValueError):
            pass
        finally:
            if dup_fd is not None:
                with suppress(OSError):
                    os.close(dup_fd)

    def _snapshot() -> str:
        with buf_lock:
            return bytes(buf[-limit:]).decode("utf-8", "replace").strip()

    t = threading.Thread(target=_read, daemon=True)
    try:
        t.start()
    except BaseException:
        if dup_fd is not None:
            with suppress(OSError):
                os.close(dup_fd)
        raise
    return t, _snapshot


def stop_proc(proc: subprocess.Popen[bytes], wait: float = 2.0) -> int:
    """Terminate a subprocess process group (`SIGTERM`, then `SIGKILL` after `wait`s)."""
    for sig, timeout_s in ((signal.SIGTERM, wait), (signal.SIGKILL, 2.0)):
        with suppress(OSError):
            if proc.poll() is None:
                os.killpg(proc.pid, sig)
        with suppress(OSError, subprocess.TimeoutExpired):
            return proc.wait(timeout=timeout_s)
    return -1 if proc.returncode is None else proc.returncode


def _read_stdout_fd(
    dup_fd: int, buf: bytearray, lock: threading.Lock, ov_ev: threading.Event, stop_ev: threading.Event
) -> None:
    poller: select.poll | None = None
    with suppress(OSError, ValueError):
        poller = select.poll()
        poller.register(dup_fd, select.POLLIN)
    try:
        while True:
            done = stop_ev.is_set()
            if poller is not None and not poller.poll(0 if done else 200):
                if done:
                    break
                continue
            chunk = os.read(dup_fd, 65536)
            if not chunk:
                break
            with lock:
                if len(buf) + len(chunk) > MAX_BATCH:
                    ov_ev.set()
                    return
                buf.extend(chunk)
    except (OSError, ValueError):
        pass
    finally:
        with suppress(OSError):
            os.close(dup_fd)


def _write_stdin_fd(dup_fd: int, data: bytes, stop_ev: threading.Event) -> None:
    poller: select.poll | None = None
    with suppress(OSError, ValueError):
        os.set_blocking(dup_fd, False)
        poller = select.poll()
        poller.register(dup_fd, select.POLLOUT)
    try:
        view = memoryview(data)
        off = 0
        total = len(view)
        while off < total and not stop_ev.is_set():
            if poller is not None and not poller.poll(200):
                continue
            try:
                written = os.write(dup_fd, view[off : off + 65536])
            except BlockingIOError:
                continue
            if written <= 0:
                break
            off += written
    except (OSError, ValueError):
        pass
    finally:
        with suppress(OSError):
            os.close(dup_fd)


def _start_fd_thread(dup_fd: int, target: Callable[..., None], *args: Any) -> threading.Thread:
    t = threading.Thread(target=target, args=(dup_fd, *args), daemon=True)
    try:
        t.start()
    except BaseException:
        with suppress(OSError):
            os.close(dup_fd)
        raise
    return t


def exec_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    stdin_data: bytes | None = None,
    stop_event: StopFlag | None = None,
    on_stderr_line: _LineHook | None = None,
    secrets: Iterable[str] = (),
) -> tuple[int, bytes, str]:
    """Run literal `argv` in a dedicated process group with optional stdin, bounded stdio, and clean teardown."""
    if not argv:
        raise Error("empty command argv")
    if stop_event is not None and stop_event.is_set():
        return 130, b"", "stopped"
    pipe = subprocess.PIPE
    deadline, out_buf, out_lock = time.monotonic() + timeout, bytearray(), threading.Lock()
    ov_ev, io_stop = threading.Event(), threading.Event()
    ov_msg = f"output exceeds {MAX_BATCH} bytes"
    try:
        proc = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=pipe if stdin_data is not None else subprocess.DEVNULL,
            stdout=pipe,
            stderr=pipe,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        return 127, b"", models.failure(error)
    with proc:
        in_t: threading.Thread | None = None
        out_t: threading.Thread | None = None
        err_t: threading.Thread | None = None
        err_snap: Callable[[], str] | None = None

        try:
            if stdin_data is not None and proc.stdin is not None:
                in_dup = os.dup(proc.stdin.fileno())
                proc.stdin.close()
                proc.stdin = None
                in_t = _start_fd_thread(in_dup, _write_stdin_fd, stdin_data, io_stop)
            if proc.stderr is not None:
                err_t, err_snap = drain_tail(proc.stderr, 65536, io_stop, on_line=on_stderr_line, secrets=secrets)
            if proc.stdout is not None:
                out_t = _start_fd_thread(
                    os.dup(proc.stdout.fileno()), _read_stdout_fd, out_buf, out_lock, ov_ev, io_stop
                )
            while True:
                if stop_event is not None and stop_event.is_set():
                    stop_proc(proc)
                    return 130, b"", "stopped"
                if ov_ev.is_set():
                    stop_proc(proc)
                    return 1, b"", ov_msg
                if (rem := deadline - time.monotonic()) <= 0:
                    stop_proc(proc)
                    return 124, b"", f"timed out after {timeout}s"
                with suppress(subprocess.TimeoutExpired):
                    rc = proc.wait(timeout=min(0.1, rem))
                    break
        except BaseException:
            stop_proc(proc)
            raise
        finally:
            io_stop.set()
            for t in (out_t, in_t, err_t):
                if t is not None:
                    t.join(timeout=2)
        with out_lock:
            out_bytes = bytes(out_buf)
        return (1, b"", ov_msg) if ov_ev.is_set() else (rc, out_bytes, err_snap() if err_snap is not None else "")


def prepare_unit(
    store: Store,
    name: str,
    base_argv: Sequence[str],
    run_id: str,
    start_iso: str,
    *,
    trigger: str = "manual",
    end_iso: str = "",
    events: Sequence[dict[str, Any]] = (),
    events_missed: int = 0,
    base_env: Mapping[str, str] | None = None,
    unit_env: Mapping[str, str] | None = None,
    unit_env_from: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    create_state: bool = True,
) -> tuple[list[str], dict[str, str], bytes | None, tuple[str, ...]]:
    """Resolve `argv`, build least-privilege `env` + `BF_*`, encode events-only `stdin` JSONL, and return secret values."""
    effective_end = end_iso or start_iso
    if create_state:
        state_root = private_dir(store, ".bf", "state").root
    else:
        try:
            state_root = private_dir(store, ".bf", "state", create=False).root
        except FileNotFoundError:
            state_root = store.root / ".bf" / "state"
    state_dir = str(state_root / name)
    stdin_bytes = b"".join(encode(ev) for ev in events) if events else None

    tokens = {
        "brain": str(store.root),
        "loop": name,
        "state": state_dir,
        "start": start_iso,
        "end": effective_end,
        "trigger": trigger,
        "run": run_id,
    }
    is_self_bf = bool(base_argv and base_argv[0] == "bf")
    if not is_self_bf:
        config.validate_loop_scripts(store, [*base_argv, *extra_args], name)
    argv = [models.PLACEHOLDER_RE.sub(lambda m: tokens.get(m[1], m[0]), tok) for tok in base_argv]
    if not is_self_bf and argv != list(base_argv):
        config.validate_loop_scripts(store, argv, name)
    if is_self_bf:
        argv = [sys.executable, "-P", "-E", "-m", "bf", *argv[1:]]
    elif argv and "/" in argv[0] and not Path(argv[0]).is_absolute():
        cleaned = argv[0].removeprefix("./")
        argv[0] = str(store.root.joinpath(*relative(cleaned)))
    if extra_args:
        argv = [*argv, *extra_args]

    env: dict[str, str] = {}
    secret_vals: list[str] = []
    for key in _ENV_ALLOWLIST:
        if val := os.environ.get(key):
            env[key] = val
            if key in _PROXY_ENV_KEYS:
                with suppress(ValueError):
                    parsed_proxy = urlsplit(val if "://" in val else f"http://{val}")
                    if pw := parsed_proxy.password:
                        if len(pw) >= 8:
                            secret_vals.append(pw)
                        if (unq := unquote(pw)) != pw and len(unq) >= 8:
                            secret_vals.append(unq)

    # Explicit bf.yaml env layers (loop env overrides brain env; empty string blanks a key cleanly).
    for layer in (base_env or {}, unit_env or {}):
        for k, v in layer.items():
            if not config.is_blocked_env_key(k):
                if v == "":
                    env.pop(k, None)
                else:
                    env[k] = v

    # Per-loop env_from overrides tracked bf.yaml values (private 0600 .env wins over host env).
    allowed_keys = tuple(dict.fromkeys(unit_env_from))
    dotenv = config.load_dotenv(store) if allowed_keys else {}
    for key in allowed_keys:
        if config.is_blocked_env_key(key):
            continue
        if key in dotenv:
            env[key] = dotenv[key]
            if len(dotenv[key]) >= 8:
                secret_vals.append(dotenv[key])
        elif (host_val := os.environ.get(key)) is not None:
            env[key] = host_val
            if len(host_val) >= 8:
                secret_vals.append(host_val)

    bf_env = {
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "BF_BRAIN": str(store.root),
        "BF_LOOP": name,
        "BF_STATE": state_dir,
        "BF_START": start_iso,
        "BF_END": effective_end,
        "BF_TRIGGER": trigger,
        "BF_RUN": run_id,
        **({"BF_EVENTS_MISSED": str(events_missed)} if events_missed > 0 else {}),
    }
    return argv, {**env, **bf_env}, stdin_bytes, tuple(dict.fromkeys(secret_vals))
