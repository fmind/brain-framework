"""Isolated subprocess execution, tool resolution, bounded tail draining, and process-group teardown."""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, cast

from bf import models
from bf.models import MAX_BATCH, Error

_S_XDG = ("CONFIG", "DATA", "CACHE", "STATE")
_S_MISE = ("DATA_DIR", "CONFIG_DIR", "CACHE_DIR", "STATE_DIR", "TRUSTED_CONFIG_PATHS")
_SHIM_ENV = ("PATH", "HOME", *(f"XDG_{k}_HOME" for k in _S_XDG), *(f"MISE_{k}" for k in _S_MISE))
_TOOL_CACHE: dict[tuple[str, ...], str] = {}
_TOOL_LOCK = threading.Lock()
type _Feeder = Callable[[IO[bytes], threading.Event], None]


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
    stream: IO[bytes], limit: int, stop: threading.Event | None = None
) -> tuple[threading.Thread, bytearray]:
    """Drain `stream` in a daemon thread while retaining at most the last `limit` bytes in memory."""
    buf, fd, poller = bytearray(), None, None
    with suppress(OSError, ValueError):
        fd, poller = stream.fileno(), select.poll()
        poller.register(fd, select.POLLIN)

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


def _feed_stdin(pipe: Any, data: bytes) -> None:
    if pipe is not None:
        try:
            with suppress(OSError):
                pipe.write(data)
                pipe.flush()
        finally:
            with suppress(OSError):
                pipe.close()


def _read_stdout(proc: subprocess.Popen[bytes], buf: bytearray, ov_ev: threading.Event) -> None:
    with suppress(OSError):
        while proc.stdout and (chunk := proc.stdout.read(65536)):
            if len(buf) + len(chunk) > MAX_BATCH:
                ov_ev.set()
                return
            buf.extend(chunk)


def exec_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    stdin_bytes: bytes = b"",
    stop_event: threading.Event | None = None,
) -> tuple[int, bytes, str]:
    """Run literal `argv` in a dedicated process group with bounded stdio, timeout, and clean teardown."""
    if not argv:
        raise Error("empty command argv")
    if stop_event is not None and stop_event.is_set():
        return 130, b"", "stopped"
    pipe, s_in = subprocess.PIPE, subprocess.PIPE if stdin_bytes else subprocess.DEVNULL
    deadline, out_buf, ov_ev = time.monotonic() + timeout, bytearray(), threading.Event()
    ov_msg = f"output exceeds {MAX_BATCH} bytes"
    try:
        proc = subprocess.Popen(  # noqa: S603
            list(argv), cwd=cwd, env=env, stdin=s_in, stdout=pipe, stderr=pipe, start_new_session=True
        )
    except (OSError, ValueError) as error:
        return 127, b"", models.failure(error)
    with proc:
        err_stop = threading.Event()
        err_t, err_buf = drain_tail(proc.stderr, 65536, err_stop) if proc.stderr else (None, bytearray())
        in_t = (
            threading.Thread(target=_feed_stdin, args=(proc.stdin, stdin_bytes), daemon=True) if stdin_bytes else None
        )
        out_t = threading.Thread(target=_read_stdout, args=(proc, out_buf, ov_ev), daemon=True)
        for t in filter(None, (in_t, out_t)):
            t.start()
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
            err_stop.set()
            for t in filter(None, (in_t, err_t)):
                t.join(timeout=2)
            if proc.stdin is not None:
                with suppress(OSError):
                    proc.stdin.close()
        err_s = bytes(err_buf).decode("utf-8", "replace").strip()
        return (1, b"", ov_msg) if ov_ev.is_set() else (rc, bytes(out_buf), err_s)
