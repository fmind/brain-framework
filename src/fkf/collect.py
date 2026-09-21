"""Explicit direct argv, bounded process groups and immutable collection."""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from fkf.config import load
from fkf.models import Collection, Error, Record, Source, decode, digest, encode, timestamp
from fkf.storage import Store, relative, writer

_STARTUP = frozenset(
    [
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "fish_function_path",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONWARNINGS",
        "PYTHONUSERBASE",
        "PYTHONPLATLIBDIR",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "RUBYGEMS_GEMDEPS",
        "GEM_PATH",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "GCONV_PATH",
        "R_ENVIRON",
        "R_ENVIRON_USER",
        "R_PROFILE",
        "R_PROFILE_USER",
    ]
)
_ROOTS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


class Runner(Protocol):
    def __call__(self, argv: list[str], source: Source, store: Store, /) -> bytes: ...


def environment(root: Path) -> dict[str, str]:
    values = dict(os.environ)

    def outside(value: str) -> bool:
        candidate = Path(value)
        try:
            return candidate.is_absolute() and not candidate.resolve().is_relative_to(root)
        except OSError, RuntimeError:
            return False

    for key in tuple(values):
        if key in _STARTUP or key.startswith(("DYLD_", "LUA_INIT", "BASH_FUNC_")):
            del values[key]
    for key in _ROOTS:
        if key in values and not outside(values[key]):
            del values[key]
    values["PATH"] = os.pathsep.join(dict.fromkeys(p for p in values.get("PATH", "").split(os.pathsep) if outside(p)))
    values["PYTHONNOUSERSITE"] = "1"
    return values


def run(argv: list[str], source: Source, store: Store) -> bytes:
    env = environment(store.root)
    executable = argv[0]
    if executable.startswith("sources/"):
        relative(executable)
        store.read(executable, 1 << 20)
        executable = str(store.root / executable)
    elif "/" in executable:
        raise Error("executable must be a bare external command or sources/ helper")
    else:
        found = shutil.which(executable, path=env["PATH"])
        if found is None:
            raise Error("required executable is unavailable on the sanitized PATH")
        executable = found
    try:
        child = subprocess.Popen(  # noqa: S603  # nosemgrep: dangerous-subprocess-use-audit
            # Only a literal executable and direct configured argv reach this boundary.
            [executable, *argv[1:]],
            cwd="/",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise Error("could not start the source command; check its executable and interpreter") from error
    output = bytearray()
    sizes = {"out": 0, "err": 0}
    deadline = time.monotonic() + source.timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((child.stdout, "out"), (child.stderr, "err")):
                if pipe is None:
                    raise Error("source pipes were not created")
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise Error("source command timed out; no evidence written")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    sizes[key.data] += len(chunk)
                    if sizes[key.data] > source.max_bytes:
                        raise Error("source output exceeded its byte limit; no evidence written")
                    if key.data == "out":
                        output.extend(chunk)
        code = child.wait()
        if code:
            raise Error(f"source command exited with status {code}; provider output remains private")
        return bytes(output)
    finally:
        # Descendants can retain pipes after their leader exits; kill the whole session.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


def collect(
    store: Store,
    name: str,
    *,
    start: str,
    end: str,
    preview: bool = False,
    automatic: bool = False,
    runner: Runner = run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    try:
        start, end = timestamp(start), timestamp(end)
    except ValueError as error:
        raise Error("collection start/end require timezone-aware timestamps") from error
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise Error("collection start must be earlier than end")

    def execute() -> dict[str, object]:
        source = load(store).sources.get(name)
        if source is None or not source.enabled:
            raise Error("source is unknown or disabled")
        values = {"base": str(store.root), "home": str(Path.home()), "start": start, "end": end}
        argv = [re.sub(r"\{\{(base|home|start|end)\}\}", lambda match: values[match[1]], arg) for arg in source.command]
        raw = runner(argv, source, store)
        if len(raw) > source.max_bytes:
            raise Error("source output exceeded its byte limit")
        try:
            records = TypeAdapter(list[Record]).validate_python(decode(raw))
            document = Collection(
                source=name,
                captured=clock().isoformat(),
                start=start,
                end=end,
                records=records,
                mode=source.mode,
                automatic=automatic,
            )
        except ValidationError as error:
            raise Error("source must emit an array of valid records with unique ids and meaningful titles") from error
        if preview:
            return {"source": name, "count": len(records), "samples": [r.model_dump() for r in records[:3]]}
        data = encode(document.model_dump())
        if len(data) > source.max_bytes:
            raise Error("normalized collection exceeds its byte limit")
        path = f"records/{name}/{digest(data)}.json"
        store.write(path, data, immutable=True)
        return {"source": name, "count": len(records), "path": path}

    if preview:
        return execute()
    with writer(store):
        return execute()
