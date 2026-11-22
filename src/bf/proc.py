"""Isolated subprocess execution, tool resolution, jq confinement, bounded tail draining, and process-group teardown."""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, cast

from bf import config, models
from bf.models import MAX_BATCH, Error, encode
from bf.storage import Store, private_dir, relative

JQ_PRELUDE = (
    'def modulemeta: error("disabled"); def modulemeta($m): error("disabled"); '
    "def get_jq_origin: null; def get_prog_origin: null; def get_search_list: []; def env: {}; {} as $ENV |"
)
_S_XDG = ("CONFIG", "DATA", "CACHE", "STATE")
_S_MISE = ("DATA_DIR", "CONFIG_DIR", "CACHE_DIR", "STATE_DIR", "TRUSTED_CONFIG_PATHS")
_SHIM_ENV = ("PATH", "HOME", *(f"XDG_{k}_HOME" for k in _S_XDG), *(f"MISE_{k}" for k in _S_MISE))
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "SHELL",
    "TERM",
    "TZ",
    "SSH_AUTH_SOCK",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    *(f"XDG_{k}_HOME" for k in _S_XDG),
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CLOUDSDK_CONFIG",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    *(p for k in ("HTTP", "HTTPS", "NO", "ALL") for p in (f"{k}_PROXY", f"{k.lower()}_proxy")),
)
_TOOL_CACHE: dict[tuple[str, ...], str] = {}
_TOOL_LOCK = threading.Lock()
type _Feeder = Callable[[IO[bytes], threading.Event], None]
type _LineHook = Callable[[bytes], None]


def resolve_tool(name: str) -> str:
    """Resolve `name` on PATH, unwrapping `mise` shims to the underlying executable."""
    if not (found := shutil.which(name)):
        raise Error(f"{name} is required on PATH; install it with mise install")
    key = (name, found, *(os.environ.get(k, "") for k in _SHIM_ENV))
    with _TOOL_LOCK:
        cached = _TOOL_CACHE.get(key)
    if cached is not None and Path(cached).is_file():
        return cached
    target, is_mise = found, False
    with suppress(OSError, subprocess.SubprocessError):
        if is_mise := (real := Path(found).resolve()).name == "mise":
            kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "cwd": "/"}
            p = subprocess.run([str(real), "which", name], **kw, env=safe_env(found), timeout=5, check=False)  # noqa: S603
            if p.returncode == 0 and (c := p.stdout.strip()) and (cp := Path(c)).is_absolute() and cp.is_file():
                target = c
    if not is_mise or target != found:
        with _TOOL_LOCK:
            if len(_TOOL_CACHE) >= 32:
                _TOOL_CACHE.pop(next(iter(_TOOL_CACHE)))
            _TOOL_CACHE[key] = target
    return target


def safe_env(binary: str = "") -> dict[str, str]:
    """Return a locale-only env for native binaries, or minimal non-secret shim vars for wrapper scripts."""
    base, is_sh = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}, False
    if binary:
        with suppress(OSError):
            if not (is_sh := (p := Path(binary)).resolve().name == "mise"):
                with p.open("rb") as h:
                    is_sh = h.read(2) == b"#!"
    return {**{k: v for k in _SHIM_ENV if (v := os.environ.get(k))}, **base} if is_sh else base


def drain_tail(
    stream: IO[bytes],
    limit: int,
    stop: threading.Event | None = None,
    *,
    on_line: _LineHook | None = None,
) -> tuple[threading.Thread, bytearray]:
    """Drain `stream` in a daemon thread, retaining the last `limit` bytes and optionally forwarding lines."""
    buf, pending, fd, poller = bytearray(), bytearray(), None, None
    with suppress(OSError, ValueError):
        fd, poller = stream.fileno(), select.poll()
        poller.register(fd, select.POLLIN)

    def _emit_line(raw_line: bytes) -> None:
        if on_line is not None and raw_line.strip():
            with suppress(Exception):
                on_line(raw_line)

    def _read() -> None:
        with suppress(OSError, ValueError):
            while True:
                done = bool(stop and stop.is_set())
                if poller is not None and not poller.poll(0 if done else 200):
                    if done:
                        break
                    continue
                if not (chunk := os.read(fd, 8192) if fd is not None else stream.read(8192)):
                    break
                buf.extend(chunk)
                if len(buf) > limit * 2:
                    del buf[:-limit]
                if on_line is not None:
                    pending.extend(chunk)
                    while (nl := pending.find(b"\n")) != -1:
                        _emit_line(bytes(pending[:nl]))
                        del pending[: nl + 1]
                    if len(pending) > 65536:
                        _emit_line(bytes(pending[:65536]))
                        del pending[:65536]
            if on_line is not None and pending:
                _emit_line(bytes(pending))
                pending.clear()
            del buf[: max(0, len(buf) - limit)]

    (t := threading.Thread(target=_read, daemon=True)).start()
    return t, buf


def stop_proc(proc: subprocess.Popen[bytes], wait: float = 2.0) -> int:
    """Terminate a subprocess process group (`SIGTERM`, then `SIGKILL` after `wait`s)."""
    for sig, tm in ((signal.SIGTERM, wait), (signal.SIGKILL, 2.0)):
        with suppress(OSError):
            proc.poll() is None and os.killpg(proc.pid, sig)
        with suppress(OSError, subprocess.TimeoutExpired):
            return proc.wait(timeout=tm)
    return -1 if proc.returncode is None else proc.returncode


def stream_proc(
    cmd: list[str], binary: str, rd: Callable[[IO[bytes]], bool], fd: _Feeder | None = None
) -> tuple[int, bytes, bool]:
    """Run a streaming read-only tool (`rg`/`jq`) with a 30s hard timeout and bounded stderr."""
    to_ev, stop_fd, err_sp, pipe = threading.Event(), threading.Event(), threading.Event(), subprocess.PIPE
    s_in = pipe if fd else subprocess.DEVNULL
    with subprocess.Popen(  # noqa: S603
        cmd, stdin=s_in, stdout=pipe, stderr=pipe, cwd="/", env=safe_env(binary), start_new_session=True
    ) as proc:
        err_t, err_buf = drain_tail(cast("IO[bytes]", proc.stderr), 8192, err_sp)

        def _on_timeout() -> None:
            to_ev.set()
            with suppress(OSError):
                proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL)

        (timer := threading.Timer(30.0, _on_timeout)).daemon = True
        timer.start()
        if feeder := (threading.Thread(target=fd, args=(proc.stdin, stop_fd), daemon=True) if fd else None):
            feeder.start()
        eof, rc = False, None
        try:
            eof = rd(cast("IO[bytes]", proc.stdout))
        finally:
            timer.cancel()
            stop_fd.set()
            with suppress(subprocess.TimeoutExpired):
                rc = proc.wait(timeout=5) if eof else None
            rc = stop_proc(proc, wait=0.0 if eof else 5.0) if rc is None else rc
            err_sp.set()
            if feeder:
                feeder.join(timeout=5)
            err_t.join(timeout=2)
        return rc, bytes(err_buf), to_ev.is_set()


def _read_stdout(proc: subprocess.Popen[bytes], buf: bytearray, ov_ev: threading.Event) -> None:
    with suppress(OSError):
        while proc.stdout and (chunk := proc.stdout.read(65536)):
            if len(buf) + len(chunk) > MAX_BATCH:
                ov_ev.set()
                return
            buf.extend(chunk)


def _write_stdin(stream: IO[bytes], data: bytes) -> None:
    with suppress(OSError):
        try:
            stream.write(data)
            stream.flush()
        finally:
            stream.close()


def exec_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    stdin_data: bytes | None = None,
    stop_event: threading.Event | None = None,
    on_stderr_line: _LineHook | None = None,
) -> tuple[int, bytes, str]:
    """Run literal `argv` in a dedicated process group with optional stdin JSON, bounded stdio, and clean teardown."""
    if not argv:
        raise Error("empty command argv")
    if stop_event is not None and stop_event.is_set():
        return 130, b"", "stopped"
    pipe = subprocess.PIPE
    deadline, out_buf, ov_ev = time.monotonic() + timeout, bytearray(), threading.Event()
    ov_msg = f"output exceeds {MAX_BATCH} bytes"
    try:
        proc = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=cwd,
            env=env,
            stdin=pipe if stdin_data is not None else subprocess.DEVNULL,
            stdout=pipe,
            stderr=pipe,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        return 127, b"", models.failure(error)
    with proc:
        in_t: threading.Thread | None = None
        if stdin_data is not None and proc.stdin is not None:
            in_t = threading.Thread(target=_write_stdin, args=(proc.stdin, stdin_data), daemon=True)
            in_t.start()
        err_stop = threading.Event()
        err_t, err_buf = (
            drain_tail(proc.stderr, 65536, err_stop, on_line=on_stderr_line) if proc.stderr else (None, bytearray())
        )
        out_t = threading.Thread(target=_read_stdout, args=(proc, out_buf, ov_ev), daemon=True)
        out_t.start()
        try:
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
            out_t.join(timeout=2)
            if in_t is not None:
                in_t.join(timeout=2)
            err_stop.set()
            if err_t is not None:
                err_t.join(timeout=2)
        err_s = bytes(err_buf).decode("utf-8", "replace").strip()
        return (1, b"", ov_msg) if ov_ev.is_set() else (rc, bytes(out_buf), err_s)


def check_jq_filter(flt: str, label: str) -> str:
    """Validate a `jq` event filter expression against an empty input in a confined subprocess."""
    try:
        jq_bin = resolve_tool("jq")
        wrapped = f"{JQ_PRELUDE}\ntry ([\n{flt}\n] | length > 0) catch false"
        cp = subprocess.run(  # noqa: S603
            [jq_bin, "-n", "-L", "/dev/null", "--", wrapped],
            capture_output=True,
            cwd="/",
            env=safe_env(jq_bin),
            timeout=5,
            check=False,
        )
    except (Error, subprocess.TimeoutExpired) as err:
        return f"{label}: {err}"
    if cp.returncode != 0:
        err_t = cp.stderr.decode("utf-8", "replace").strip()
        return f"{label}: invalid jq event filter ({err_t.splitlines()[-1] if err_t else 'jq exited nonzero'})"
    return ""


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
    base_env: Mapping[str, str] | None = None,
    unit_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str], bytes]:
    """Resolve `argv`, build layered `BF_*` env, and encode JSON context bytes for stdin."""
    effective_end = end_iso or start_iso
    state_dir = str(private_dir(store).root / name)
    ctx_bytes = encode(
        {
            "run": run_id,
            "trigger": trigger,
            "loop": name,
            "start": start_iso,
            "end": effective_end,
            "events": list(events),
        }
    )
    tokens = {
        "brain": str(store.root),
        "loop": name,
        "state": state_dir,
        "start": start_iso,
        "end": effective_end,
        "trigger": trigger,
        "run": run_id,
    }
    argv = [models.PLACEHOLDER_RE.sub(lambda m: tokens.get(m[1], m[0]), tok) for tok in base_argv]
    if argv and argv[0] == "bf":
        argv = [sys.executable, "-m", "bf", *argv[1:]]
    elif argv and "/" in argv[0] and not Path(argv[0]).is_absolute():
        cleaned = argv[0].removeprefix("./")
        parts = relative(cleaned)
        if not store.exists(cleaned):
            raise Error(f"{name}: command {argv[0]} does not exist inside brain")
        argv[0] = str(store.root.joinpath(*parts))
    for tok in argv[1:]:
        cleaned = tok.removeprefix(f"{store.root}/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if cleaned.startswith(("sensors/", "routines/", "agents/")) and not store.exists(cleaned):
            raise Error(f"{name}: script {cleaned} must be a regular file inside brain")

    env: dict[str, str] = {key: val for key in _ENV_ALLOWLIST if (val := os.environ.get(key))}
    env |= dict(base_env or {}) | dict(unit_env or {}) | config.load_dotenv(store)
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
    }
    return argv, {**env, **bf_env}, ctx_bytes
