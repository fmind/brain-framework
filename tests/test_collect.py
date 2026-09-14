"""Collection is atomic, explicit, bounded and never logs provider content."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fkf.collect import collect, environment, run
from fkf.models import Error, Source
from fkf.storage import Store

START = "2026-09-01T00:00:00.000000Z"
END = "2026-09-02T00:00:00.000000Z"


def setup(base: Store) -> None:
    base.write(
        "fkf.yaml",
        b"version: 1\nid: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n  sample: {command: [echo, '{{start}}', '{{end}}', '{{base}}']}\n",
    )


def test_collect_preview_and_durable_write(base: Store) -> None:
    setup(base)

    def fake(argv: list[str], source: Source, store: Store) -> bytes:
        assert argv[1:] == [START, END, str(base.root)]
        assert source.timeout == 120
        assert store.root == base.root
        return b'[{"id":"x","title":"Decision","text":"Preserve evidence"}]'

    before = base.files("records")
    preview = collect(base, "sample", start=START, end=END, runner=fake, preview=True)
    assert preview["count"] == 1
    assert base.files("records") == before

    def clock():
        return datetime(2026, 9, 2, tzinfo=UTC)

    first = collect(base, "sample", start=START, end=END, runner=fake, clock=clock)
    second = collect(base, "sample", start=START, end=END, runner=fake, clock=clock)
    assert first == second
    assert len(base.files("records")) == len(before) + 1


@pytest.mark.parametrize(
    "raw", [b"not json", b"{}", b'[{"id":"a","title":""}]', b'[{"id":"a","title":"A"},{"id":"a","title":"B"}]']
)
def test_failed_collection_writes_nothing(base: Store, raw: bytes) -> None:
    setup(base)
    before = base.files("records")
    with pytest.raises(Error):
        collect(base, "sample", start=START, end=END, runner=lambda *_: raw)
    assert base.files("records") == before


def test_collection_uses_current_configuration(base: Store) -> None:
    setup(base)
    calls = []

    def fake(argv: list[str], *_: object) -> bytes:
        calls.append(argv)
        return b"[]"

    collect(base, "sample", start=START, end=END, runner=fake)
    base.write("fkf.local.yaml", b"sources:\n  sample: {command: [echo, changed]}\n")
    collect(base, "sample", start=START, end=END, runner=fake)
    assert calls == [["echo", START, END, str(base.root)], ["echo", "changed"]]


def test_collection_rejects_invalid_window_and_unavailable_source(base: Store) -> None:
    setup(base)
    for start, end in [(END, START), (START, START), ("bad", END)]:
        with pytest.raises(Error):
            collect(base, "sample", start=start, end=end)
    with pytest.raises(Error, match="unknown"):
        collect(base, "missing", start=START, end=END)
    base.write("fkf.local.yaml", b"sources:\n  sample: {enabled: false}\n")
    with pytest.raises(Error, match="disabled"):
        collect(base, "sample", start=START, end=END)


def test_process_environment_excludes_base_and_startup_inputs(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f".:{base.root}:/usr/bin:/bin:/usr/bin")
    for name in ["PYTHONPATH", "BASH_ENV", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "LUA_INIT", "BASH_FUNC_injected%%"]:
        monkeypatch.setenv(name, "bad")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base.root / "config"))
    values = environment(base.root)
    assert values["PATH"] == "/usr/bin:/bin"
    assert values["PYTHONNOUSERSITE"] == "1"
    assert not any(
        k in values
        for k in ["PYTHONPATH", "BASH_ENV", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "LUA_INIT", "XDG_CONFIG_HOME"]
    )


def test_real_process_success_failure_timeout_and_output_limits(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    source = Source(command=["sh"], timeout=1, max_bytes=128)
    assert run(["sh", "-c", "printf '[]'"], source, base) == b"[]"
    for script, match in [
        ("printf private-secret >&2; exit 7", "status 7"),
        ("sleep 5", "timed out"),
        ("while :; do printf abc; done", "byte limit"),
        ("while :; do printf abc >&2; done", "byte limit"),
    ]:
        with pytest.raises(Error, match=match) as failure:
            run(["sh", "-c", script], source, base)
        assert "private-secret" not in str(failure.value)
    with pytest.raises(Error, match="unavailable"):
        run(["no-such-fkf-command"], source, base)
    with pytest.raises(Error, match="bare external"):
        run(["/bin/sh"], source, base)


def test_reviewed_helper_executes_from_neutral_directory(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    base.write("sources/test.sh", b"#!/bin/sh\npwd\n")
    (base.root / "sources/test.sh").chmod(0o700)
    assert run(["sources/test.sh"], Source(command=["sources/test.sh"]), base) == b"/\n"
    base.write("sources/bad.sh", b"#!/nonexistent/interpreter\n")
    (base.root / "sources/bad.sh").chmod(0o700)
    with pytest.raises(Error, match="interpreter"):
        run(["sources/bad.sh"], Source(command=["sources/bad.sh"]), base)


def test_placeholder_values_are_opaque_not_expanded_twice(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    setup(base)
    home = base.root.parent / "literal-{{start}}"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    base.write(
        "fkf.yaml",
        b"version: 1\nid: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n  sample: {command: [echo, '{{home}}']}\n",
    )

    def fake(argv: list[str], *_: object) -> bytes:
        assert argv == ["echo", str(home)]
        return b"[]"

    assert collect(base, "sample", start=START, end=END, preview=True, runner=fake)["count"] == 0
