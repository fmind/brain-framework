"""Tests for the bf v19 CLI surface, JSON/JSONL/text/raw outputs, sanitized stderr, and exit codes (0/1/2/130/143)."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bf import __version__, cli, engine, index, state
from bf import main as bf_main
from bf.models import FORMAT, MAX_BATCH, NOTES_FILE
from bf.storage import Store


def test_cli_commands_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `bf init .` derives the brain name from `.resolve().name`!
    dot_dir = tmp_path / "dot-brain"
    dot_dir.mkdir()
    monkeypatch.chdir(dot_dir)
    assert cli.run(["init", "."]) == 0
    dot_reply = json.loads(capsys.readouterr().out)
    assert dot_reply == {"ok": True, "brain": "dot-brain", "path": str(dot_dir.resolve())}
    monkeypatch.chdir(tmp_path)

    target = tmp_path / "my-brain"
    assert cli.run(["init", str(target), "--name", "cli-demo"]) == 0
    init_reply = json.loads(capsys.readouterr().out)
    assert init_reply == {"ok": True, "brain": "cli-demo", "path": str(target.resolve())}

    # Optional domain schemas: ingest works WITHOUT `schemas/<source>.json` (core schema still enforced)!
    stdin_payload = b'[{"id":"m-1","title":"Stdin item","text":"hello"}]\n'
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 0
    ing_reply = json.loads(capsys.readouterr().out)
    assert (ing_reply["ok"], ing_reply["source"], ing_reply["added"], ing_reply["dry_run"]) == (
        True,
        "notes-feed",
        1,
        False,
    )

    # Explicit `--schema NAME` enforces `schemas/<NAME>.json` (and fails closed when absent).
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--schema", "missing", "--brain", str(target)]) == 1
    assert "explicitly requested schema not found" in capsys.readouterr().err

    (target / "schemas" / "shared.json").write_text('{"type":"object","required":["owner"]}\n')
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--schema", "shared", "--brain", str(target)]) == 1
    assert "missing required 'owner'" in capsys.readouterr().err

    # Ingest from explicit `--input FILE` with `--dry-run`.
    batch_file = tmp_path / "batch.json"
    batch_file.write_text('[{"id":"m-2","title":"File item","owner":"alice"}]\n')
    assert (
        cli.run(
            [
                "ingest",
                "notes-feed",
                "--input",
                str(batch_file),
                "--schema",
                "shared",
                "--dry-run",
                "--brain",
                str(target),
            ]
        )
        == 0
    )
    dry_reply = json.loads(capsys.readouterr().out)
    assert (dry_reply["added"], dry_reply["dry_run"]) == (1, True)

    # Missing `--input` file -> exit 1 with sanitized stderr.
    assert cli.run(["ingest", "notes-feed", "--input", str(tmp_path / "missing.json"), "--brain", str(target)]) == 1
    assert "bf: cannot read input file" in capsys.readouterr().err

    # Interactive TTY stdin without `--input` -> exit 2.
    class _TtyStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _TtyStdin(""))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 2
    assert "stdin is a terminal" in capsys.readouterr().err

    # Oversized stdin -> exit 1.
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x" * (MAX_BATCH + 1)), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 1
    assert "exceeds" in capsys.readouterr().err

    # Removed commands & flags -> exit 2.
    for removed_cmd in (
        ["new", "concept", "x"],
        ["action", "Audit"],
        ["post", "hi"],
        ["validate"],
        ["eval"],
        ["mcp", "--brain", str(target)],
        ["ingest", "notes-feed", "--to", "usages", "--brain", str(target)],
        ["query", "--jq", ".", "--brain", str(target)],
        ["query", "--sql", "SELECT 1", "--brain", str(target)],
        ["read", "concepts/welcome.md", "--raw", "--brain", str(target)],
        ["status", "--watch", "--brain", str(target)],
        ["status", "--all", "--brain", str(target)],
        ["watch", "--format", "tui", "--brain", str(target)],
        ["run", "--prune", "--brain", str(target)],
        ["run", "--reset", "x", "--brain", str(target)],
    ):
        assert cli.run(removed_cmd) == 2, removed_cmd
        capsys.readouterr()

    # Universal bundle `<slug>/index.md` builds cleanly (`--dry-run` + real build) and ignores sibling attachments!
    act_dir = target / "actions" / "audit-cli"
    (act_dir / "inputs").mkdir(parents=True)
    (act_dir / "inputs" / "raw.md").write_text("# Ignored attachment\n")
    (act_dir / "index.md").write_text(
        "---\ntype: action\nstatus: open\ncreated: 2026-11-16\n---\n\n# Audit CLI\n\n- [ ] Check help text\n"
    )
    assert cli.run(["build", "--dry-run", "--brain", str(target)]) == 0
    b_dry = json.loads(capsys.readouterr().out)
    assert (b_dry["ok"], b_dry["dry_run"], b_dry["notes"], b_dry["written"]) == (True, True, 2, 1)
    assert cli.run(["build", "--brain", str(target)]) == 0
    b_real = json.loads(capsys.readouterr().out)
    assert (b_real["ok"], b_real["dry_run"], b_real["notes"]) == (True, False, 2)

    # Pre- and post-subcommand `--brain`, conflicts, empty `--brain`, and `--brain` on `init`.
    assert cli.run(["--brain", str(target), "read", "bf:///concepts/welcome.md#conventions"]) == 0
    assert "{rel=...}" in json.loads(capsys.readouterr().out)["text"]
    assert cli.run(["read", "concepts/welcome.md", "--format", "raw", "--brain", str(target)]) == 0
    assert "# Welcome" in capsys.readouterr().out
    assert (
        cli.run(
            [
                "--brain",
                str(target),
                "read",
                "bf:///concepts/welcome.md#conventions",
                "--brain",
                str(tmp_path / "other"),
            ]
        )
        == 2
    )
    assert "conflicting --brain" in capsys.readouterr().err
    assert cli.run(["read", "concepts/welcome.md", "--brain", ""]) == 2
    assert "empty" in capsys.readouterr().err
    assert cli.run(["--brain", str(target), "init", str(tmp_path / "b2")]) == 2
    assert "does not accept --brain" in capsys.readouterr().err

    # `read` requires TARGET and rejects `#fragment` on records (exit 2).
    assert cli.run(["read", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["read", "notes-feed:m-1#.title", "--brain", str(target)]) == 2
    capsys.readouterr()

    # `query` supports FTS5 BM25, structured filters, `--format json|jsonl|text`, and `--` for dash-prefixed queries.
    assert cli.run(["query", "Conventions", "-s", "concepts", "-n", "5", "--brain", str(target)]) == 0
    q_fts = json.loads(capsys.readouterr().out)
    assert q_fts["engine"] == "fts5"
    assert q_fts["count"] >= 1
    assert q_fts["truncated"] is False

    assert cli.run(["query", "--kind", "note", "--format", "text", "--brain", str(target)]) == 0
    assert "concepts/welcome.md" in capsys.readouterr().out

    assert cli.run(["query", "--brain", str(target), "--", "-unknown"]) == 0
    capsys.readouterr()

    assert cli.run(["query", "--brain", str(target)]) == 2
    assert "pass QUERY or at least one filter" in capsys.readouterr().err
    assert cli.run(["review", "--brain", str(target), "--", "extra"]) == 2
    capsys.readouterr()

    # Configure loops to exercise `run` (exact, glob, `--all`, `--due`, `--dry-run`, guards).
    (target / "bf.yaml").write_text(
        f"version: {FORMAT}\nname: cli-demo\nloops:\n  check-a:\n    run: [bf, check]\n  check-b:\n    run: [bf, check]\n"
    )
    assert cli.run(["run", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["run", "check-a", "--due", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--due", "--brain", str(target), "--", "x"]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--all", "--brain", str(target), "--", "x"]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--dry-run", "check-*", "--brain", str(target)]) == 0
    glob_out = json.loads(capsys.readouterr().out)
    assert glob_out["dry_run"] is True
    assert [r["loop"] for r in glob_out["results"]] == ["check-a", "check-b"]
    assert cli.run(["run", "--dry-run", "--all", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ran"] == 2
    assert cli.run(["--brain", str(target), "run", "--due"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["check", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["status", "--brain", str(target)]) == 0
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["ok"] is True
    assert set(st_out["counts"]) == {"notes", "memories", "sources", "loops"}
    assert st_out["counts"]["notes"] == 2
    assert st_out["attention"] == []
    assert "loops" not in st_out
    assert cli.run(["status", "--loops", "--brain", str(target)]) == 0
    assert "check-a" in json.loads(capsys.readouterr().out)["loops"]
    assert cli.run(["status", "--format", "text", "--brain", str(target)]) == 0
    assert "HEALTHY" in capsys.readouterr().out
    assert cli.run(["status", "--check", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # `emit` supports `--dedupe` (reporting `deduped: bool`) and `--dry-run`, rejecting non-object `--data` or missing id!
    assert (
        cli.run(
            [
                "emit",
                "custom.ping",
                "--source",
                "agent",
                "--data",
                '{"id":"p1","x":1}',
                "--dry-run",
                "--brain",
                str(target),
            ]
        )
        == 0
    )
    em_dry = json.loads(capsys.readouterr().out)
    assert (em_dry["ok"], em_dry["dry_run"]) == (True, True)
    assert (
        cli.run(
            [
                "emit",
                "custom.ping",
                "--source",
                "agent",
                "--data",
                '{"id":"p1","x":1}',
                "--dedupe",
                "--brain",
                str(target),
            ]
        )
        == 0
    )
    em_out = json.loads(capsys.readouterr().out)
    assert (em_out["ok"], em_out["deduped"], em_out["dry_run"]) == (True, False, False)
    assert em_out["event"]["kind"] == "custom.ping"
    assert (
        cli.run(
            [
                "emit",
                "custom.ping",
                "--source",
                "agent",
                "--data",
                '{"id":"p1","x":1}',
                "--dedupe",
                "--brain",
                str(target),
            ]
        )
        == 0
    )
    em_dup = json.loads(capsys.readouterr().out)
    assert em_dup["deduped"] is True
    assert em_dup["event"]["seq"] == em_out["event"]["seq"]
    assert cli.run(["emit", "custom.ping", "--dedupe", "--brain", str(target)]) == 2
    assert "requires a non-empty string 'id'" in capsys.readouterr().err
    assert cli.run(["emit", "custom.default", "--brain", str(target)]) == 0
    em_def = json.loads(capsys.readouterr().out)["event"]
    assert (em_def["source"], em_def["data"]) == ("bf", {})
    assert cli.run(["emit", "custom.ping", "--data", "{bad", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["emit", "custom.ping", "--data", "[1,2]", "--brain", str(target)]) == 2
    capsys.readouterr()

    # `watch` delegates to `watch.serve` and returns `exit_code`.
    called_watch: list[tuple[Path, str]] = []

    def fake_watch_serve(store: Store, *, fmt: str = "auto") -> dict[str, object]:
        called_watch.append((store.root, fmt))
        return {"ok": True, "brain": "cli-demo", "exit_code": 0 if len(called_watch) == 1 else 143}

    monkeypatch.setattr("bf.watch.serve", fake_watch_serve)
    assert cli.run(["watch", "--format", "jsonl", "--brain", str(target)]) == 0
    assert cli.run(["watch", "--brain", str(target)]) == 143
    assert called_watch == [(target.resolve(), "jsonl"), (target.resolve(), "auto")]

    assert cli.run(["review", "-n", "10", "--brain", str(target)]) == 0
    rv_ok = json.loads(capsys.readouterr().out)
    assert rv_ok["ok"] is True
    assert rv_ok["brain"] == "cli-demo"

    # Broken note makes `build` and `check` exit 1; corrupt memory JSONL (without index) makes `review` exit 1.
    (target / "concepts" / "broken.md").write_text("---\nid: forbidden\n---\n# Bad\n")
    assert cli.run(["build", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["problems"]
    assert cli.run(["check", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    (target / "memories" / "corrupt.jsonl").write_text('{"links":[{"to":"x"}]}\n{bad\n')
    (target / ".bf" / "cache" / index.INDEX_NAME).unlink(missing_ok=True)
    assert cli.run(["review", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    (target / "memories" / "corrupt.jsonl").unlink()

    # Invalid flag, abbreviated subcommand flag, or reserved source -> exit 2.
    assert cli.run(["--unknown-flag"]) == 2
    assert "bf: invalid input:" in capsys.readouterr().err
    assert cli.run(["query", "--sc", "concepts", "--brain", str(target)]) == 2
    assert "bf: invalid input:" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"[]"), encoding="utf-8"))
    assert cli.run(["ingest", "concepts", "--brain", str(target)]) == 2
    assert "reserved" in capsys.readouterr().err

    # KeyboardInterrupt -> exit 130.
    monkeypatch.setattr(engine, "review", lambda *_a, **_kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.run(["review", "--brain", str(target)]) == 130


def test_cli_schema_state_formats_and_sigterm_exit_143(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BF_BRAIN", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    lazy_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from bf import cli; assert cli.run(['schema', 'bf']) == 0; "
                "assert not any(m in sys.modules for m in ('bf.engine', 'bf.markdown', 'bf.query', 'bf.records', 'bf.index'))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert lazy_proc.returncode == 0
    assert json.loads(lazy_proc.stdout)["title"] == "Config"
    assert cli.run(["schema"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"bf", "record"}
    for bad_s in ("unknown", "evals", "concepts", "usages", ""):
        assert cli.run(["schema", bad_s]) == 2
        capsys.readouterr()
    assert cli.run(["--brain", str(tmp_path), "schema", "bf"]) == 2
    capsys.readouterr()
    assert cli.run(["init", str(tmp_path / "empty-name"), "--name", ""]) == 2
    capsys.readouterr()
    assert cli.run(["init", str(tmp_path / "bool-name"), "--name", "true", "--description", "Custom desc."]) == 0
    assert json.loads(capsys.readouterr().out)["brain"] == "true"
    assert "Custom desc." in (tmp_path / "bool-name" / "bf.yaml").read_text()
    assert cli.run(["init", str(tmp_path / "bad-desc"), "--description", "x" * 1025]) == 2
    capsys.readouterr()

    b = tmp_path / "f19"
    assert cli.run(["init", str(b), "--name", "f19"]) == 0
    capsys.readouterr()
    (b / "concepts" / "topic-a.md").write_text("---\ntype: architecture\nstatus: draft\n---\n\n# Topic A\n")
    (b / "projects" / "bundle-proj").mkdir(parents=True)
    (b / "projects" / "bundle-proj" / "index.md").write_text(
        "---\ntype: project\nstatus: active\ncreated: 2026-11-15\n---\n\n# Bundle Proj\n"
    )
    (b / "concepts" / "stale-tab.md").write_text(
        "---\ntype: guide\nstatus: stable\nupdated: 2020-01-01\nstale_after: 30d\n---\n\n# Stale\tTab\n"
    )
    assert cli.run(["build", "--brain", str(b)]) == 0
    capsys.readouterr()

    # Unified `bf read` returns `"ok": True` + compiled record or `--format raw`.
    assert cli.run(["read", "concepts/topic-a.md", "--brain", str(b)]) == 0
    rd_rec = json.loads(capsys.readouterr().out)
    assert (rd_rec["ok"], rd_rec["kind"], rd_rec["record"]["ref"]) == (True, "note", "concepts/topic-a.md")
    assert cli.run(["read", "concepts/topic-a.md", "--format", "raw", "--brain", str(b)]) == 0
    assert "# Topic A" in capsys.readouterr().out
    assert cli.run(["read", "concepts/topic-a.md", "--format", "raw", "--max-bytes", "15", "--brain", str(b)]) == 0
    raw_cap = capsys.readouterr()
    assert len(raw_cap.out.encode("utf-8")) <= 15
    assert "truncated" in raw_cap.err
    assert cli.run(["read", "concepts/topic-a.md", "--max-bytes", "260", "--brain", str(b)]) == 0
    rd_cap = json.loads(capsys.readouterr().out)
    assert rd_cap["truncated"] is True
    assert "record" in rd_cap
    assert cli.run(["read", "concepts/topic-a.md", "--max-bytes", "140", "--brain", str(b)]) == 2
    assert "too small" in capsys.readouterr().err

    # `bf query --kind note --format jsonl` returns compact summary rows and warns on truncation & stale index/unbuilt!
    assert cli.run(["query", "--kind", "note", "-n", "1", "--format", "jsonl", "--brain", str(b)]) == 0
    q_trunc = capsys.readouterr()
    assert len(q_trunc.out.splitlines()) == 1
    assert "truncated" in q_trunc.err
    assert cli.run(["query", "--kind", "note", "--format", "jsonl", "--brain", str(b)]) == 0
    jsonl_rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(r["ref"] == "concepts/topic-a.md" and "text" not in r for r in jsonl_rows)

    assert cli.run(["review", "--format", "jsonl", "--brain", str(b)]) == 0
    rev_jsonl = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(r["ref"] == "concepts/stale-tab.md" for r in rev_jsonl)
    assert cli.run(["review", "--format", "tsv", "--brain", str(b)]) == 2
    capsys.readouterr()

    assert cli.run(["check", "--all", "--strict", "--brain", str(b)]) == 0
    chk = json.loads(capsys.readouterr().out)
    assert chk["ok"] is True
    assert chk["counts"]["notes"] == 4
    assert cli.run(["check", "--notes", "--format", "text", "--brain", str(b)]) == 0
    assert "[OK]" in capsys.readouterr().out
    assert cli.run(["review", "--format", "text", "--brain", str(b)]) == 0
    rev_txt = capsys.readouterr().out
    assert "STALE" in rev_txt
    assert "concepts/stale-tab.md" in rev_txt
    assert cli.run(["check", "-n", "10", "--all", "--brain", str(b)]) == 2
    capsys.readouterr()

    # Unbuilt note fails strictly read-only `bf check` without writing `memories/_notes.jsonl`.
    (b / "concepts" / "unbuilt.md").write_text("---\ntype: guide\nstatus: stable\n---\n# Unbuilt\n")
    before_jsonl = (b / NOTES_FILE).read_bytes()
    assert cli.run(["check", "--brain", str(b)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert cli.run(["check", "--format", "text", "--brain", str(b)]) == 1
    assert "FAILED" in capsys.readouterr().out
    assert (b / NOTES_FILE).read_bytes() == before_jsonl
    (b / "concepts" / "unbuilt.md").unlink()
    assert cli.run(["build", "--brain", str(b)]) == 0
    capsys.readouterr()

    # Unbuilt folder hint on `bf status` (text) and `bf query --format jsonl` stderr:
    (b / "actions" / "fresh.md").write_text("---\ntype: action\nstatus: open\ncreated: 2026-11-22\n---\n# Fresh\n")
    assert cli.run(["status", "--format", "text", "--brain", str(b)]) == 0
    assert "unbuilt notes: actions" in capsys.readouterr().out
    assert cli.run(["query", "--kind", "note", "--format", "jsonl", "--brain", str(b)]) == 0
    q_unbuilt = capsys.readouterr()
    assert "unbuilt notes in actions" in q_unbuilt.err
    assert cli.run(["query", "--kind", "note", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["unbuilt_notes"] == ["actions"]
    (b / "actions" / "fresh.md").unlink()

    assert cli.run(["emit", "BAD_KIND", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["emit", "ok.ev", "--source", "BAD_SRC", "--brain", str(b)]) == 2
    capsys.readouterr()

    # `bf emit --data -` (stdin JSON).
    class _StdinBuf:
        def __init__(self, payload: bytes, tty: bool = False) -> None:
            self._payload, self._tty = payload, tty

        @property
        def buffer(self) -> io.BytesIO:
            return io.BytesIO(self._payload)

        def isatty(self) -> bool:
            return self._tty

    monkeypatch.setattr(sys, "stdin", _StdinBuf(b'{"id":"e-in","ok":true}\n'))
    assert cli.run(["emit", "stdin.ev", "--data", "-", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["event"]["data"]["id"] == "e-in"
    monkeypatch.setattr(sys, "stdin", _StdinBuf(b"{bad"))
    assert cli.run(["emit", "stdin.ev", "--data", "-", "--brain", str(b)]) == 2
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", _StdinBuf(b"", tty=True))
    assert cli.run(["emit", "stdin.ev", "--data", "-", "--brain", str(b)]) == 2
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", _StdinBuf(b"   \n"))
    assert cli.run(["emit", "stdin.ev", "--data", "-", "--brain", str(b)]) == 2
    capsys.readouterr()

    # `bf status` exits 0 when readable (reporting `ok: false`); `bf status --check` exits 1 when attention is non-empty!
    (b / "bf.yaml").write_text(f'version: {FORMAT}\nname: f19\nloops:\n  miss:\n    run: ["false"]\n')
    (b / ".bf" / "state").mkdir(parents=True, mode=0o700, exist_ok=True)
    (b / ".bf" / "state" / "miss.json").write_text('{"name":"miss","status":"failed","consecutive_failures":2}\n')
    (b / ".bf" / "state" / "miss.json").chmod(0o600)
    assert cli.run(["status", "--loops", "--brain", str(b)]) == 0
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["ok"] is False
    assert st_out["problems"]
    assert cli.run(["status", "--check", "--loops", "--brain", str(b)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False

    # `bf review --format jsonl` emits sanitized stderr diagnostic on broken JSONL while keeping stdout pure.
    (b / "memories" / "badsrc.jsonl").write_text('{"links":[{"to":"x"}]}\n{broken\n')
    (b / ".bf" / "cache" / index.INDEX_NAME).unlink(missing_ok=True)
    assert cli.run(["review", "--format", "jsonl", "--brain", str(b)]) == 1
    cap = capsys.readouterr()
    assert cap.err.startswith("bf: ")
    assert "badsrc.jsonl" in cap.err
    (b / "memories" / "badsrc.jsonl").unlink()

    # `bf state prune` and `bf state reset` (including `--brain` before action, `--dry-run`, orphan dirs/locks/logs, and cursor anchoring):
    (b / ".bf" / "state" / "orphan-loop.json").write_text('{"name":"orphan-loop","status":"failed"}\n')
    (b / ".bf" / "state" / "orphan-loop.json").chmod(0o600)
    (b / ".bf" / "state" / "orphan-dir").mkdir(mode=0o700)
    (b / ".bf" / "state" / "orphan-dir" / "state.txt").write_text("x")
    (b / ".bf" / "locks").mkdir(parents=True, mode=0o700, exist_ok=True)
    (b / ".bf" / "locks" / "loop-orphan-loop.lock").write_text("")
    (b / "logs" / "orphan-loop.jsonl").write_text("{}\n")
    (b / "logs" / "orphan-loop.jsonl").chmod(0o600)
    assert cli.run(["state", "--brain", str(b), "prune", "--dry-run"]) == 0
    prune_dry = json.loads(capsys.readouterr().out)
    assert prune_dry["dry_run"] is True
    assert "orphan-loop" in prune_dry["pruned"]["states"]
    assert "orphan-dir" in prune_dry["pruned"]["dirs"]
    assert "loop-orphan-loop.lock" in prune_dry["pruned"]["locks"]
    assert "orphan-loop" in prune_dry["pruned"]["logs"]
    assert (b / ".bf" / "state" / "orphan-loop.json").is_file()
    assert cli.run(["state", "prune", "--brain", str(b)]) == 0
    pruned_out = json.loads(capsys.readouterr().out)
    assert pruned_out["dry_run"] is False
    assert not (b / ".bf" / "state" / "orphan-loop.json").exists()
    assert not (b / ".bf" / "state" / "orphan-dir").exists()
    assert not (b / ".bf" / "locks" / "loop-orphan-loop.lock").exists()
    assert not (b / "logs" / "orphan-loop.jsonl").exists()

    assert cli.run(["state", "reset", "miss", "--dry-run", "--brain", str(b)]) == 0
    reset_dry = json.loads(capsys.readouterr().out)
    assert (reset_dry["dry_run"], reset_dry["reset"]) == (True, ["miss"])
    assert cli.run(["state", "reset", "miss", "--brain", str(b)]) == 0
    reset_ok = json.loads(capsys.readouterr().out)
    assert (reset_ok["dry_run"], reset_ok["reset"]) == (False, ["miss"])
    # Reset anchors `event_seq` to current `latest_event_seq()` instead of deleting to 0!
    reset_doc = json.loads((b / ".bf" / "state" / "miss.json").read_text())
    assert reset_doc["status"] == "idle"
    assert reset_doc["event_seq"] == reset_ok["event_seq"]

    # CLI `query --links-to` and `query --all-sources` flags round-trip cleanly:
    (b / "concepts" / "linker.md").write_text(
        "---\ntype: guide\nstatus: stable\n---\n# Linker\n\nSee [Topic A](topic-a.md).\n"
    )
    assert cli.run(["build", "--brain", str(b)]) == 0
    capsys.readouterr()
    assert cli.run(["query", "--links-to", "concepts/topic-a.md", "--brain", str(b)]) == 0
    assert [it["ref"] for it in json.loads(capsys.readouterr().out)["items"]] == ["concepts/linker.md"]
    assert cli.run(["query", "Topic A", "--all-sources", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] >= 1
    assert cli.run(["state", "reset", "--all", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["reset"] == ["miss"]
    assert cli.run(["state", "reset", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["state", "reset", "no-such", "--brain", str(b)]) == 2
    capsys.readouterr()

    # `bf run` handles SIGTERM cleanly and exits `143`!
    (b / "routines" / "waiter.py").write_text(
        "import pathlib, time\npathlib.Path('routines/waiter.up').write_text('1')\ntime.sleep(10)\n"
    )
    (b / "bf.yaml").write_text(
        f"version: {FORMAT}\nname: f19\nloops:\n  waiter:\n    run: [{sys.executable!r}, routines/waiter.py]\n"
    )
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "bf", "run", "waiter", "--brain", str(b)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as sig_proc:
        try:
            dl = time.monotonic() + 5.0
            while time.monotonic() < dl and not (b / "routines" / "waiter.up").exists():
                time.sleep(0.02)
            assert (b / "routines" / "waiter.up").exists()
            sig_proc.send_signal(signal.SIGTERM)
            assert sig_proc.wait(timeout=5) == 143
        finally:
            if sig_proc.poll() is None:
                sig_proc.kill()
                sig_proc.wait(timeout=2)

    with pytest.raises(SystemExit) as help_exc:
        cli.run(["read", "--help"])
    assert help_exc.value.code == 0
    help_txt = capsys.readouterr().out
    assert "raw UTF-8 text" in help_txt
    assert "Maximum UTF-8 response size" in help_txt

    # Closed stdin (`sys.stdin = None`, `.closed`, raising `ValueError` on `isatty()`, or missing `.buffer`) fails fast with exit 2!
    closed_io = io.StringIO("")
    closed_io.close()

    class _ErrTty:
        closed = False

        def isatty(self) -> bool:
            raise ValueError("closed fd")

    class _NoBuf:
        closed = False

        def isatty(self) -> bool:
            return False

    for bad_stdin in (None, closed_io, _ErrTty(), _NoBuf()):
        monkeypatch.setattr(sys, "stdin", bad_stdin)
        assert cli.run(["ingest", "closed-src", "--brain", str(b)]) == 2
        assert "stdin is closed" in capsys.readouterr().err
        assert cli.run(["emit", "closed.ev", "--data", "-", "--brain", str(b)]) == 2
        assert "stdin is closed" in capsys.readouterr().err

    # Best-effort SQLite cache failure during `bf build` and `bf ingest` surfaces `index_warning` on stdout & stderr!
    with monkeypatch.context() as mp_w:
        mp_w.setattr(index, "sync_all", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("cache readonly")))
        mp_w.setattr(index, "sync_source_delta", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("cache readonly")))
        (b / "concepts" / "warn-idx.md").write_text("---\ntype: guide\nstatus: stable\n---\n# Warn\n")
        assert cli.run(["build", "--brain", str(b)]) == 0
        cap_bw = capsys.readouterr()
        assert "inaccessible file or directory" in json.loads(cap_bw.out)["index_warning"]
        assert "bf: warning: inaccessible file or directory" in cap_bw.err
        monkeypatch.setattr(sys, "stdin", _StdinBuf(b'[{"id":"w1","title":"W"}]\n'))
        assert cli.run(["ingest", "warn-src", "--brain", str(b)]) == 0
        cap_iw = capsys.readouterr()
        assert "inaccessible file or directory" in json.loads(cap_iw.out)["index_warning"]
        assert "bf: warning: inaccessible file or directory" in cap_iw.err

    # Empty-string flags and empty `bf init ""` fail fast with exit 2 instead of silently dropping filters/schemas!
    for empty_argv in (
        ["init", ""],
        ["init", "   "],
        ["ingest", "empty-src", "--schema", "", "--brain", str(b)],
        ["query", "Topic", "--scope", "", "--brain", str(b)],
        ["query", "Topic", "--type", "", "--brain", str(b)],
        ["query", "Topic", "--status", "", "--brain", str(b)],
        ["query", "Topic", "--tag", "", "--brain", str(b)],
        ["query", "Topic", "--since", "", "--brain", str(b)],
        ["query", "Topic", "--links-to", "", "--brain", str(b)],
        ["query", "Topic", "--exclude", "", "--brain", str(b)],
        ["emit", "empty.ev", "--data", "", "--brain", str(b)],
        ["emit", "empty.ev", "--data", "   ", "--brain", str(b)],
    ):
        assert cli.run(empty_argv) == 2
        capsys.readouterr()

    # `bf state` missing action includes `--help` hint; `bf state reset --help` uses `event_seq` (no internal fn name)!
    assert cli.run(["state"]) == 2
    assert "bf state --help" in capsys.readouterr().err
    with pytest.raises(SystemExit) as st_help:
        cli.run(["state", "reset", "--help"])
    assert st_help.value.code == 0
    st_help_txt = capsys.readouterr().out
    assert "latest_event_seq" not in st_help_txt
    assert "event_seq" in st_help_txt

    # `bf emit --in|--at`, `bf events`, and `bf pause`/`bf resume` CLI round-trips and error exits!
    assert (
        cli.run(
            ["emit", "wake.check", "--in", "30m", "--data", '{"id":"wk-1"}', "--dedupe", "--dry-run", "--brain", str(b)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["scheduled"] is True
    assert cli.run(["emit", "wake.check", "--in", "30m", "--data", '{"id":"wk-1"}', "--dedupe", "--brain", str(b)]) == 0
    tmr_out = json.loads(capsys.readouterr().out)
    assert (tmr_out["scheduled"], tmr_out["deduped"]) == (True, False)
    assert (
        cli.run(["emit", "wake.check", "--at", "2026-12-01T09:00:00Z", "--data", '{"id":"wk-2"}', "--brain", str(b)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["timer"]["deliver_at"] == "2026-12-01T09:00:00Z"
    for bad_em in (
        ["emit", "wake.check", "--in", "0s", "--brain", str(b)],
        ["emit", "wake.check", "--at", "2026-12-01", "--brain", str(b)],
        ["emit", "wake.check", "--in", "5m", "--at", "2026-12-01T09:00:00Z", "--brain", str(b)],
    ):
        assert cli.run(bad_em) == 2
        capsys.readouterr()

    assert cli.run(["events", "--brain", str(b)]) == 0
    ev_j = json.loads(capsys.readouterr().out)
    assert (ev_j["ok"], ev_j["mode"], ev_j["pending_timers"]) == (True, "events", 2)
    assert cli.run(["events", "--timers", "--format", "text", "--brain", str(b)]) == 0
    assert "wake.check" in capsys.readouterr().out
    assert cli.run(["events", "--format", "text", "--brain", str(b)]) == 0
    assert "Events: f19" in capsys.readouterr().out
    assert cli.run(["events", "--format", "jsonl", "-n", "1", "--brain", str(b)]) == 0
    capsys.readouterr()
    assert cli.run(["events", "--since", "0", "-n", "1", "--brain", str(b)]) == 0
    ev_s0 = json.loads(capsys.readouterr().out)
    assert ev_s0["items"][0]["seq"] == 1
    assert cli.run(["events", "--since", "999999", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    for bad_ev in (
        ["events", "--timers", "--since", "0", "--brain", str(b)],
        ["events", "--timers", "--since", "1", "--brain", str(b)],
        ["events", "--kind", "", "--brain", str(b)],
        ["events", "--source", "", "--brain", str(b)],
        ["events", "--since", "-5", "--brain", str(b)],
        ["emit", "loop.wake", "--in", "9" * 5000 + "s", "--brain", str(b)],
    ):
        assert cli.run(bad_ev) == 2
        capsys.readouterr()
    assert (
        cli.run(
            ["emit", "loop.big", "--data", '{"x":"' + ("a" * (state.MAX_EVENT_LINE + 16)) + '"}', "--brain", str(b)]
        )
        == 1
    )
    assert "exceeds" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", _StdinBuf(b"x" * (state.MAX_EVENT_LINE + 16)))
    assert cli.run(["emit", "loop.big", "--data", "-", "--brain", str(b)]) == 1
    assert "exceeds" in capsys.readouterr().err

    assert cli.run(["pause", "waiter", "--dry-run", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] == ["waiter"]
    assert cli.run(["pause", "waiter", "--for", "2h", "--brain", str(b)]) == 0
    p_out = json.loads(capsys.readouterr().out)
    assert p_out["paused"] == ["waiter"]
    assert p_out["paused_until"]
    assert cli.run(["status", "--format", "text", "--brain", str(b)]) == 0
    assert "paused" in capsys.readouterr().out
    assert cli.run(["resume", "waiter", "--dry-run", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["resumed"] == ["waiter"]
    assert cli.run(["resume", "waiter", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["resumed"] == ["waiter"]
    for bad_p in (
        ["pause", "--brain", str(b)],
        ["pause", "waiter", "--for", "0s", "--brain", str(b)],
        ["resume", "--brain", str(b)],
    ):
        assert cli.run(bad_p) == 2
        capsys.readouterr()


def test_python_m_bf_entrypoint_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "bf", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == __version__

    b = tmp_path / "entry"
    monkeypatch.setattr(sys, "argv", ["bf", "init", str(b)])
    with pytest.raises(SystemExit) as exc:
        bf_main()
    assert exc.value.code == 0
    capsys.readouterr()

    # `--brain` before `run` with `--` passthrough and non-regular `--input` FIFO rejection.
    (b / "bf.yaml").write_text(f"version: {FORMAT}\nname: entry\nloops:\n  echo-p:\n    run: [echo, base]\n")
    assert cli.run(["--brain", str(b), "run", "echo-p", "--dry-run", "--", "--flag"]) == 0
    dry_p = json.loads(capsys.readouterr().out)
    assert dry_p["results"][0]["argv"][-1] == "--flag"

    fifo_p = tmp_path / "in.fifo"
    os.mkfifo(fifo_p)
    assert cli.run(["ingest", "src1", "--input", str(fifo_p), "--brain", str(b)]) == 2
    assert "regular file" in capsys.readouterr().err

    with monkeypatch.context() as mp:
        mp.setattr("os.name", "nt")
        with pytest.raises(SystemExit) as nt_exc:
            bf_main()
        assert "Linux or macOS" in str(nt_exc.value)


def test_cli_validation_order_jsonl_hints_and_broken_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bad CLI args exit 2 BEFORE brain/config lookup or I/O; jsonl query hints; broken status keeps inside_brain."""
    non_brain = tmp_path / "not-a-brain"
    non_brain.mkdir()
    broken = tmp_path / "broken-brain"
    broken.mkdir()
    (broken / "bf.yaml").write_text("version: 999\nname: broken\n")
    monkeypatch.delenv("BF_BRAIN", raising=False)
    monkeypatch.chdir(broken)

    for target_brain in (str(non_brain), str(broken)):
        for argv in (
            ["pause", "--brain", target_brain],
            ["pause", "a", "--all", "--brain", target_brain],
            ["pause", "a", "--for", "0s", "--brain", target_brain],
            ["resume", "--brain", target_brain],
            ["state", "reset", "--brain", target_brain],
            ["run", "--brain", target_brain],
            ["events", "--since", "-1", "--brain", target_brain],
            ["events", "--kind", "BAD", "--brain", target_brain],
            ["events", "--source", "BAD", "--brain", target_brain],
            ["events", "--limit", "0", "--brain", target_brain],
            ["emit", "BAD KIND", "--brain", target_brain],
            ["check", "--limit", "0", "--brain", target_brain],
            ["review", "--limit", "0", "--brain", target_brain],
            ["read", "", "--brain", target_brain],
            ["read", "#anchor", "--brain", target_brain],
            ["read", "#", "--brain", target_brain],
            ["ingest", "src1", "--input", "", "--brain", target_brain],
            ["ingest", "BAD_SRC", "--input", str(tmp_path / "missing.json"), "--brain", target_brain],
            ["query", "--brain", target_brain],
            ["query", "--limit", "0", "--brain", target_brain],
            ["query", "--tag", "  ", "--brain", target_brain],
        ):
            assert cli.run(argv) == 2, argv
            capsys.readouterr()

    # Broken `bf.yaml` status preserves `inside_brain: True` and folds secondary `events.jsonl` errors into `problems`.
    assert cli.run(["status", "--brain", str(broken)]) == 0
    st_broken = json.loads(capsys.readouterr().out)
    assert st_broken["ok"] is False
    assert st_broken["inside_brain"] is True
    assert any("version" in p for p in st_broken["problems"])

    (broken / ".bf" / "state").mkdir(parents=True, mode=0o700)
    (broken / ".bf").chmod(0o700)
    (broken / ".bf" / "state").chmod(0o700)
    ev_bad = broken / ".bf" / "state" / state.EVENTS_FILE
    ev_bad.write_text("{}\n")
    ev_bad.chmod(0o644)
    try:
        assert cli.run(["status", "--brain", str(broken)]) == 0
        st_double = json.loads(capsys.readouterr().out)
        assert st_double["ok"] is False
        assert st_double["events_seq"] == 0
        assert len(st_double["problems"]) >= 2
    finally:
        ev_bad.chmod(0o600)

    # `bf query --format jsonl` surfaces `excluded by bf.yaml:` and stale index hints on stderr; `bf status --format text` surfaces unbuilt hint.
    qb = tmp_path / "qhint"
    assert cli.run(["init", str(qb)]) == 0
    capsys.readouterr()
    (qb / "bf.yaml").write_text(f"version: {FORMAT}\nname: qhint\nsearch:\n  exclude: [telemetry]\n")
    (qb / ".bf" / "cache" / "index.sqlite3").unlink()
    (qb / "concepts" / "extra.md").write_text("---\ntype: guide\nstatus: stable\n---\n# Extra\n")
    assert cli.run(["query", "welcome", "--format", "jsonl", "--brain", str(qb)]) == 0
    captured = capsys.readouterr()
    assert "excluded by bf.yaml: telemetry" in captured.err
    assert "search index stale" in captured.err
    assert cli.run(["review", "--format", "jsonl", "--brain", str(qb)]) == 0
    rev_err = capsys.readouterr().err
    assert "unbuilt notes in concepts" in rev_err
    assert cli.run(["read", "#only-fragment", "--brain", str(qb)]) == 2
    capsys.readouterr()
