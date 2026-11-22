"""Tests for the bf CLI surface, JSON stdout, sanitized stderr, and exit codes (0/1/2/130)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bf import __version__, cli, engine
from bf import main as bf_main
from bf.models import FORMAT, MAX_BATCH
from bf.storage import Store

OBJ_SCHEMA = b'{"type":"object"}\n'


def test_cli_commands_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "my-brain"
    assert cli.run(["init", str(target), "--name", "cli-demo"]) == 0
    init_reply = json.loads(capsys.readouterr().out)
    assert init_reply == {"ok": True, "brain": "cli-demo", "path": str(target.resolve())}

    # Ingest without schemas/<source>.json fails closed (exit 1)!
    stdin_payload = b'[{"id":"m-1","title":"Stdin item","text":"hello"}]\n'
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 1
    assert "missing required schema" in capsys.readouterr().err

    # Add required schema and ingest from stdin (`--input -`).
    (target / "schemas" / "notes-feed.json").write_bytes(OBJ_SCHEMA)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 0
    ing_reply = json.loads(capsys.readouterr().out)
    assert (ing_reply["ok"], ing_reply["source"], ing_reply["added"]) == (True, "notes-feed", 1)

    # Ingest from explicit `--input FILE` with `--dry-run`.
    batch_file = tmp_path / "batch.json"
    batch_file.write_text('[{"id":"m-2","title":"File item"}]\n')
    assert cli.run(["ingest", "notes-feed", "--input", str(batch_file), "--dry-run", "--brain", str(target)]) == 0
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

    # Removed `new`, `action`, `post`, `validate`, `eval` commands -> exit 2.
    for removed_cmd in (["new", "concept", "x"], ["action", "Audit"], ["post", "hi"], ["validate"], ["eval"]):
        assert cli.run(removed_cmd) == 2
        capsys.readouterr()

    # Universal bundle `<slug>/index.md` builds cleanly and ignores sibling attachments!
    act_dir = target / "actions" / "audit-cli"
    (act_dir / "inputs").mkdir(parents=True)
    (act_dir / "inputs" / "raw.md").write_text("# Ignored attachment\n")
    (act_dir / "index.md").write_text(
        "---\ntype: action\nstatus: open\ncreated: 2026-11-16\n---\n\n# Audit CLI\n\n- [ ] Check help text\n"
    )
    assert cli.run(["build", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == 2

    # Pre- and post-subcommand `--brain`, conflicts, empty `--brain`, and `--brain` on `init`.
    assert cli.run(["--brain", str(target), "read", "bf:///concepts/welcome.md#conventions"]) == 0
    assert "attr_list" in json.loads(capsys.readouterr().out)["text"]
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

    # `query` supports `-s/--scope`, `-n/--limit`, `--`, and rejects removed flags.
    assert cli.run(["query", "Conventions", "-s", "concepts", "-n", "5", "--brain", str(target)]) == 0
    q_rg = json.loads(capsys.readouterr().out)
    assert q_rg["count"] >= 1
    assert q_rg["truncated"] is False

    assert cli.run(["query", "--jq", 'select(.kind=="note") | .ref', "--brain", str(target)]) == 0
    assert "concepts/welcome.md" in json.loads(capsys.readouterr().out)["items"]

    assert cli.run(["query", "--brain", str(target), "--", "-unknown"]) == 0
    capsys.readouterr()

    assert cli.run(["query", "--sql", "SELECT 1", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["query", "--brain", str(target)]) == 2
    assert "pass PATTERN, --jq EXPR" in capsys.readouterr().err
    assert cli.run(["query", "Conventions", "--jq", ".ref", "--brain", str(target)]) == 0
    assert "concepts/welcome.md" in json.loads(capsys.readouterr().out)["items"]
    assert cli.run(["review", "--brain", str(target), "--", "extra"]) == 2
    capsys.readouterr()

    # Configure one loop to exercise `run` (exact, glob, `--all`, `--due`, `--dry-run`, guards).
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
    assert [r["loop"] for r in glob_out["results"]] == ["check-a", "check-b"]
    assert cli.run(["run", "--dry-run", "--all", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ran"] == 2
    assert cli.run(["--brain", str(target), "run", "--due"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["mcp", "--brain", str(target)]) == 2
    capsys.readouterr()

    assert cli.run(["check", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.run(["check", "--eval", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["check", "--validate", "--brain", str(target)]) == 2
    capsys.readouterr()

    assert cli.run(["status", "--check", "--brain", str(target)]) == 0
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["ok"] is True
    assert set(st_out["counts"]) == {"notes", "records", "usages", "sources", "loops"}
    assert st_out["counts"]["notes"] == 2
    assert st_out["attention"] == []
    assert cli.run(["status", "--format", "text", "--brain", str(target)]) == 0
    assert "HEALTHY" in capsys.readouterr().out

    # `emit` appends custom events (and rejects non-object `--data`).
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
    assert em_out["ok"] is True
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
    assert json.loads(capsys.readouterr().out)["event"]["seq"] == em_out["event"]["seq"]
    assert cli.run(["emit", "custom.default", "--brain", str(target)]) == 0
    em_def = json.loads(capsys.readouterr().out)["event"]
    assert (em_def["source"], em_def["data"]) == ("bf", {})
    assert cli.run(["emit", "custom.ping", "--data", "{bad", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["emit", "custom.ping", "--data", "[1,2]", "--brain", str(target)]) == 2
    capsys.readouterr()

    # `watch` delegates to `watch.serve`.
    called_watch: list[tuple[Path, str]] = []

    def fake_watch_serve(store: Store, *, fmt: str = "auto") -> dict[str, object]:
        called_watch.append((store.root, fmt))
        return {"ok": len(called_watch) == 1, "brain": "cli-demo"}

    monkeypatch.setattr("bf.watch.serve", fake_watch_serve)
    assert cli.run(["watch", "--format", "jsonl", "--brain", str(target)]) == 0
    assert cli.run(["watch", "--brain", str(target)]) == 1
    assert called_watch == [(target.resolve(), "jsonl"), (target.resolve(), "auto")]

    assert cli.run(["review", "-n", "10", "--brain", str(target)]) == 0
    rv_ok = json.loads(capsys.readouterr().out)
    assert rv_ok["ok"] is True
    assert rv_ok["brain"] == "cli-demo"

    # Broken note makes `build` and `check` exit 1; corrupt sensor JSONL makes `review` exit 1.
    (target / "concepts" / "broken.md").write_text("---\nid: forbidden\n---\n# Bad\n")
    assert cli.run(["build", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["problems"]
    assert cli.run(["check", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    (target / "memories" / "corrupt.jsonl").write_text('{"links":[{"to":"x"}]}\n{bad\n')
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


def test_cli_schema_read_query_review_and_hygiene(
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
                "assert not any(m in sys.modules for m in ('bf.engine', 'bf.markdown', 'bf.query', 'bf.records'))"
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
    for bad_s in ("unknown", "evals", "concepts", ""):
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

    b = tmp_path / "f17"
    assert cli.run(["init", str(b), "--name", "f17"]) == 0
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

    # Unified `bf read` returns `"ok": True` + compiled record or `--raw`.
    assert cli.run(["read", "concepts/topic-a.md", "--brain", str(b)]) == 0
    rd_rec = json.loads(capsys.readouterr().out)
    assert (rd_rec["ok"], rd_rec["kind"], rd_rec["record"]["ref"]) == (True, "note", "concepts/topic-a.md")
    assert cli.run(["read", "concepts/topic-a.md", "--raw", "--brain", str(b)]) == 0
    assert "# Topic A" in json.loads(capsys.readouterr().out)["text"]
    assert cli.run(["read", "concepts/topic-a.md", "--max-bytes", "90", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["truncated"] is True

    # `bf query --kind note` returns compact summary rows (not full `text` bodies!) and warns on jsonl truncation.
    assert cli.run(["query", "--kind", "note", "-n", "1", "--format", "jsonl", "--brain", str(b)]) == 0
    q_trunc = capsys.readouterr()
    assert len(q_trunc.out.splitlines()) == 1
    assert "truncated" in q_trunc.err
    assert cli.run(["query", "--kind", "note", "--format", "jsonl", "--brain", str(b)]) == 0
    jsonl_rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(r["ref"] == "concepts/topic-a.md" and "text" not in r for r in jsonl_rows)
    assert cli.run(["query", "--jq", ".ref", "--format", "jsonl", "--brain", str(b)]) == 0
    assert '"concepts/topic-a.md"' in capsys.readouterr().out.splitlines()

    assert cli.run(["review", "--format", "jsonl", "--brain", str(b)]) == 0
    rev_jsonl = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(r["ref"] == "concepts/stale-tab.md" for r in rev_jsonl)
    assert cli.run(["review", "--format", "tsv", "--brain", str(b)]) == 2
    capsys.readouterr()

    assert cli.run(["check", "--all", "--strict", "--brain", str(b)]) == 0
    chk = json.loads(capsys.readouterr().out)
    assert chk["ok"] is True
    assert chk["notes"] == 4
    assert cli.run(["check", "--notes", "--format", "text", "--brain", str(b)]) == 0
    assert "[OK]" in capsys.readouterr().out
    assert cli.run(["review", "--format", "text", "--brain", str(b)]) == 0
    rev_txt = capsys.readouterr().out
    assert "STALE" in rev_txt
    assert "concepts/stale-tab.md" in rev_txt
    assert cli.run(["check", "-n", "10", "--all", "--brain", str(b)]) == 2
    capsys.readouterr()

    # Unbuilt note fails strictly read-only `bf check` without writing `memories/concepts.jsonl`.
    (b / "concepts" / "unbuilt.md").write_text("---\ntype: guide\nstatus: stable\n---\n# Unbuilt\n")
    before_jsonl = (b / "memories" / "concepts.jsonl").read_bytes()
    assert cli.run(["check", "--brain", str(b)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert cli.run(["check", "--format", "text", "--brain", str(b)]) == 1
    assert "FAILED" in capsys.readouterr().out
    assert (b / "memories" / "concepts.jsonl").read_bytes() == before_jsonl
    (b / "concepts" / "unbuilt.md").unlink()
    assert cli.run(["build", "--brain", str(b)]) == 0
    capsys.readouterr()

    # Unbuilt folder hint on `bf status` (JSON & text) and `bf query` stderr:
    (b / "actions" / "fresh.md").write_text("---\ntype: action\nstatus: open\ncreated: 2026-11-22\n---\n# Fresh\n")
    assert cli.run(["status", "--format", "text", "--brain", str(b)]) == 0
    assert "unbuilt notes: actions" in capsys.readouterr().out
    assert cli.run(["query", "--kind", "note", "--brain", str(b)]) == 0
    q_unbuilt = capsys.readouterr()
    assert "unbuilt notes in actions" in q_unbuilt.err
    assert json.loads(q_unbuilt.out)["unbuilt_notes"] == ["actions"]
    (b / "actions" / "fresh.md").unlink()

    assert cli.run(["emit", "BAD_KIND", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["emit", "ok.ev", "--source", "BAD_SRC", "--brain", str(b)]) == 2
    capsys.readouterr()

    # `bf emit --data -` (stdin JSON), `bf status --format text`, and `bf ingest --to usages`:
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
    monkeypatch.setattr(
        sys, "stdin", _StdinBuf(b'[{"id":"u1","title":"LLM call","model":"m","input_tokens":5,"cost_usd":0.01}]\n')
    )
    assert cli.run(["ingest", "llm", "--to", "usages", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["target"] == "usages"
    assert cli.run(["query", "--kind", "usage", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert cli.run(["read", "llm:u1", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "usage"
    assert cli.run(["status", "--format", "text", "--brain", str(b)]) == 0
    st_text = capsys.readouterr().out
    assert "Brain: f17" in st_text
    assert "usages=1" in st_text
    assert cli.run(["--brain", str(b), "run", "no-such", "--", "--flag"]) == 2
    capsys.readouterr()
    assert cli.run(["ingest", "llm", "--input", str(tmp_path / "missing.jsonl"), "--brain", str(b)]) == 1
    capsys.readouterr()
    assert cli.run(["ingest", "llm", "--to", "invalid", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["watch", "--format", "invalid", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["status", "--format", "invalid", "--brain", str(b)]) == 2
    capsys.readouterr()

    # `bf status --check` exits 1 when a loop failed.
    (b / "bf.yaml").write_text(f'version: {FORMAT}\nname: f17\nloops:\n  miss:\n    run: ["false"]\n')
    (b / ".bf" / "miss.json").write_text('{"name":"miss","status":"failed","consecutive_failures":2}\n')
    (b / ".bf" / "miss.json").chmod(0o600)
    assert cli.run(["status", "--all", "--check", "--brain", str(b)]) == 1
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["ok"] is False
    assert st_out["problems"]

    # `bf review --format jsonl` emits sanitized stderr diagnostic on broken sensor JSONL while keeping stdout pure.
    (b / "memories" / "badsrc.jsonl").write_text('{"links":[{"to":"x"}]}\n{broken\n')
    assert cli.run(["review", "--format", "jsonl", "--brain", str(b)]) == 1
    cap = capsys.readouterr()
    assert cap.err.startswith("bf: ")
    assert "badsrc.jsonl" in cap.err
    (b / "memories" / "badsrc.jsonl").unlink()

    # `bf run --prune` and `bf run --reset` (including `--dry-run`):
    (b / ".bf" / "orphan-loop.json").write_text('{"name":"orphan-loop","status":"failed"}\n')
    (b / ".bf" / "orphan-loop.json").chmod(0o600)
    assert cli.run(["run", "--prune", "--dry-run", "--brain", str(b)]) == 0
    prune_dry = json.loads(capsys.readouterr().out)
    assert prune_dry["dry_run"] is True
    assert "orphan-loop" in prune_dry["pruned"]
    assert (b / ".bf" / "orphan-loop.json").is_file()
    assert cli.run(["run", "--prune", "miss", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--prune", "--brain", str(b), "--", "extra"]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--prune", "--brain", str(b)]) == 0
    pruned_out = json.loads(capsys.readouterr().out)
    assert "orphan-loop" in pruned_out["pruned"]
    assert not (b / ".bf" / "orphan-loop.json").exists()

    assert cli.run(["run", "--reset", "miss", "--dry-run", "--brain", str(b)]) == 0
    reset_dry = json.loads(capsys.readouterr().out)
    assert reset_dry["dry_run"] is True
    assert reset_dry["reset"] == ["miss"]
    assert (b / ".bf" / "miss.json").is_file()
    assert cli.run(["run", "--reset", "miss", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["reset"] == ["miss"]
    assert not (b / ".bf" / "miss.json").exists()
    assert cli.run(["run", "--reset", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--reset", "no-such", "--brain", str(b)]) == 2
    capsys.readouterr()

    with pytest.raises(SystemExit) as help_exc:
        cli.run(["read", "--help"])
    assert help_exc.value.code == 0
    help_txt = capsys.readouterr().out
    assert "raw Markdown" in help_txt
    assert "Maximum UTF-8 JSON response size" in help_txt


def test_python_m_bf_entrypoint_and_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr("os.name", "nt")
    with pytest.raises(SystemExit, match="Linux or macOS"):
        bf_main()
