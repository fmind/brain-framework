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


def test_cli_commands_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "my-brain"
    assert cli.run(["init", str(target), "--name", "cli-demo"]) == 0
    init_reply = json.loads(capsys.readouterr().out)
    assert init_reply == {"brain": "cli-demo", "path": str(target.resolve())}

    # Ingest from stdin (`--input -`).
    stdin_payload = b'[{"id":"m-1","title":"Stdin item","text":"hello"}]\n'
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_payload), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 0
    ing_reply = json.loads(capsys.readouterr().out)
    assert (ing_reply["source"], ing_reply["added"]) == ("notes-feed", 1)

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

    # Removed `action` and `post` commands -> exit 2.
    assert cli.run(["action", "Audit CLI"]) == 2
    capsys.readouterr()
    assert cli.run(["post", "hello"]) == 2
    capsys.readouterr()

    # Plain Markdown under `actions/` builds cleanly with `bf build`!
    (target / "actions" / "2026-11-16_audit-cli").mkdir(parents=True)
    (target / "actions" / "2026-11-16_audit-cli" / "ACTION.md").write_text(
        "---\ntype: action\nstatus: draft\n---\n\n# Audit CLI\n\n- [ ] Check help text\n"
    )
    assert cli.run(["build", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == 2

    # Pre- and post-subcommand `--brain`, conflicts, empty `--brain`, and `--brain` on `init`.
    assert cli.run(["--brain", str(target), "read", "bf:///concepts/welcome.md#conventions"]) == 0
    assert "attr_list" in json.loads(capsys.readouterr().out)["markdown"]
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

    # `read` requires TARGET and rejects `#fragment` on JSON/records (exit 2).
    assert cli.run(["read", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["read", "notes-feed:m-1#.title", "--brain", str(target)]) == 2
    capsys.readouterr()

    # `query` supports `-s/--scope`, `-n/--limit`, `--` for leading-dash patterns, and rejects `--sql`.
    assert cli.run(["query", "Conventions", "-s", "concepts", "-n", "5", "--brain", str(target)]) == 0
    q_rg = json.loads(capsys.readouterr().out)
    assert q_rg["count"] >= 1
    assert q_rg["truncated"] is False

    assert cli.run(["query", "--jq", 'select(.kind=="concept") | .ref', "--brain", str(target)]) == 0
    assert "concepts/welcome.md" in json.loads(capsys.readouterr().out)["items"]

    assert cli.run(["query", "--brain", str(target), "--", "-unknown"]) == 0
    capsys.readouterr()

    assert cli.run(["query", "--sql", "SELECT 1", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["query", "--brain", str(target)]) == 2
    assert "pass QUERY, --jq FILTER" in capsys.readouterr().err
    assert cli.run(["query", "Conventions", "--jq", ".ref", "--brain", str(target)]) == 0
    assert "concepts/welcome.md" in json.loads(capsys.readouterr().out)["items"]
    assert cli.run(["review", "--brain", str(target), "--", "extra"]) == 2
    capsys.readouterr()

    # `run` requires JOB/--due/--hook; guards `-- EXTRA_ARGS`.
    assert cli.run(["run", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["run", "build", "--due", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--due", "--brain", str(target), "--", "x"]) == 2
    capsys.readouterr()
    assert cli.run(["run", "--dry-run", "build", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "dry_run"
    assert cli.run(["run", "--hook", "pre-commit", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.run(["--brain", str(target), "run", "--due"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # `mcp` serves `stdio` only and rejects removed `--transport` and `--port` flags (exit 2).
    called_mcp: list[str] = []
    monkeypatch.setattr("bf.mcp.serve", lambda b: called_mcp.append(b))
    assert cli.run(["mcp", "--brain", str(target)]) == 0
    assert called_mcp == [str(target)]
    assert cli.run(["mcp", "--brain", str(target), "--transport", "http"]) == 2
    capsys.readouterr()
    assert cli.run(["mcp", "--brain", str(target), "--port", "9000"]) == 2
    capsys.readouterr()

    assert cli.run(["eval", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["status", "--check", "--brain", str(target)]) == 0
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["ok"] is True
    assert set(st_out["counts"]) == {"notes", "records", "sources", "loops"}
    assert st_out["counts"]["notes"] == 2
    assert st_out["attention"] == []

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

    # `watch --once` is removed (exit 2); `watch` delegates to `watch.serve`.
    assert cli.run(["watch", "--once", "--brain", str(target)]) == 2
    capsys.readouterr()
    called_watch: list[tuple[Path, bool]] = []

    def fake_watch_serve(store: Store, *, json_stream: bool = False) -> dict[str, object]:
        called_watch.append((store.root, json_stream))
        return {"ok": len(called_watch) == 1, "brain": "cli-demo"}

    monkeypatch.setattr("bf.watch.serve", fake_watch_serve)
    assert cli.run(["watch", "--json", "--brain", str(target)]) == 0
    assert cli.run(["watch", "--brain", str(target)]) == 1
    assert called_watch == [(target.resolve(), True), (target.resolve(), False)]

    assert cli.run(["build", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == 2

    assert cli.run(["validate", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["review", "--brain", str(target)]) == 0
    rv_ok = json.loads(capsys.readouterr().out)
    assert rv_ok["ok"] is True
    assert rv_ok["brain"] == "cli-demo"

    # Broken note makes `build`, `validate`, and `review` exit 1 while still emitting compact JSON.
    (target / "concepts" / "broken.md").write_text("---\nid: forbidden\n---\n# Bad\n")
    assert cli.run(["build", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["problems"]
    assert cli.run(["validate", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert cli.run(["review", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False

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


def test_cli_format15_commands_and_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BF_BRAIN", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    # Lazy imports: `bf schema` works without a brain and skips heavy modules.
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
    assert json.loads(lazy_proc.stdout)["title"]
    assert cli.run(["schema"]) == 0
    assert set(json.loads(capsys.readouterr().out)) >= {"bf", "record", "concepts", "evals"}
    assert cli.run(["schema", "unknown"]) == 2
    capsys.readouterr()
    assert cli.run(["schema", ""]) == 2
    assert "bf: invalid input:" in capsys.readouterr().err
    assert cli.run(["--brain", str(tmp_path), "schema", "bf"]) == 2
    capsys.readouterr()
    assert cli.run(["init", str(tmp_path / "empty-name"), "--name", ""]) == 2
    capsys.readouterr()

    b = tmp_path / "f15"
    assert cli.run(["init", str(b), "--name", "f15"]) == 0
    capsys.readouterr()
    assert (
        cli.run(
            [
                "new",
                "concept",
                "topic-a",
                "--title",
                "Topic A",
                "--type",
                "architecture",
                "--status",
                "draft",
                "--brain",
                str(b),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["path"] == "concepts/topic-a.md"
    assert "type: architecture" in (b / "concepts" / "topic-a.md").read_text()
    assert cli.run(["build", "--force", "--brain", str(b)]) == 0
    capsys.readouterr()
    # Seed a stale note and a draft action (with a tab in title) to exercise review/query formatting thoroughly.
    (b / "concepts" / "stale-tab.md").write_text(
        "---\ntype: guide\nstatus: stable\nupdated: 2020-01-01\nstale_after: 30d\n---\n\n# Stale\tTab\n"
    )
    assert cli.run(["new", "action", "open-act", "--title", "Open Act", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    # Invalid `bf new` title (invalid attr_list) or concept schema value exits 2 without creating the file.
    assert cli.run(["new", "concept", "bad-title", "--title", "Demo {#a?b=1}", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert not (b / "concepts" / "bad-title.md").exists()
    assert cli.run(["new", "concept", "bad-st", "--status", "bogus", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert not (b / "concepts" / "bad-st.md").exists()

    assert cli.run(["read", "concepts/topic-a.md", "--outline", "--brain", str(b)]) == 0
    rd_out = json.loads(capsys.readouterr().out)
    assert rd_out["headings"]
    assert "text" not in rd_out
    assert cli.run(["read", "concepts/topic-a.md", "--lines", "1:3", "--brain", str(b)]) == 0
    assert "type: architecture" in json.loads(capsys.readouterr().out)["content"]
    assert cli.run(["read", "concepts/topic-a.md", "--raw", "--brain", str(b)]) == 0
    assert "# Topic A" in json.loads(capsys.readouterr().out)["markdown"]
    assert (
        cli.run(
            ["read", "concepts/topic-a.md", "--full", "--fields", "ref,kind", "--max-bytes", "500", "--brain", str(b)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"ref": "concepts/topic-a.md", "kind": "concept"}

    assert cli.run(["query", "--kind", "note", "--fields", "ref,title", "--format", "jsonl", "--brain", str(b)]) == 0
    jsonl_rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert jsonl_rows
    assert all(set(r) == {"ref", "title"} for r in jsonl_rows)
    assert any(r["ref"] == "concepts/topic-a.md" for r in jsonl_rows)
    assert cli.run(["query", "--jq", ".ref", "--format", "tsv", "--brain", str(b)]) == 0
    assert "concepts/topic-a.md" in capsys.readouterr().out.splitlines()
    assert cli.run(["query", "Stale", "--format", "tsv", "--fields", "ref,title", "--brain", str(b)]) == 0
    assert capsys.readouterr().out.strip() == "concepts/stale-tab.md\tStale Tab"
    assert cli.run(["query", "no-such-match-xyz", "--format", "tsv", "--brain", str(b)]) == 0
    assert capsys.readouterr().out == ""
    assert cli.run(["query", "--scope", "   ", "--brain", str(b)]) == 2
    capsys.readouterr()

    assert cli.run(["review", "--format", "jsonl", "--brain", str(b)]) == 0
    rev_jsonl = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert {r["section"] for r in rev_jsonl} == {"stale", "action_open"}
    assert cli.run(["review", "--format", "tsv", "--fields", "section,ref,title", "--brain", str(b)]) == 0
    rev_tsv = [ln.split("\t") for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert ["stale", "concepts/stale-tab.md", "Stale Tab"] in rev_tsv
    assert any(row[0] == "action_open" and row[2] == "Open Act" for row in rev_tsv)
    assert (
        cli.run(["review", "--limit", "1", "--all", "--fields", "ref,title", "--format", "tsv", "--brain", str(b)]) == 0
    )
    rev_proj = [ln.split("\t") for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert all(len(row) == 2 for row in rev_proj)

    assert cli.run(["eval", "smoke.yaml", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.run(["validate", "--limit", "10", "--all", "--strict", "--force", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.run(["status", "--all", "--check", "--fields", "brain", "--brain", str(b)]) == 0
    assert json.loads(capsys.readouterr().out) == {"brain": "f15"}
    assert cli.run(["check", "--strict", "--force", "--brain", str(b)]) == 0
    chk = json.loads(capsys.readouterr().out)
    assert chk["ok"] is True
    assert set(chk) >= {"build", "validate", "eval"}
    with pytest.raises(SystemExit):
        cli.run(["--help"])
    assert "DEFAULT_BRAIN_PATH" in capsys.readouterr().out

    (b / "concepts" / "broken-chk.md").write_text("---\nid: forbidden\n---\n# Bad\n")
    assert cli.run(["check", "--brain", str(b)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    (b / "concepts" / "broken-chk.md").unlink()
    assert cli.run(["build", "--brain", str(b)]) == 0
    capsys.readouterr()
    assert cli.run(["read", "concepts/topic-a.md", "--outline", "--raw", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["read", "concepts/topic-a.md", "--brain", str(b), "--", "extra"]) == 2
    capsys.readouterr()
    assert cli.run(["emit", "BAD_KIND", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["emit", "ok.ev", "--source", "BAD_SRC", "--brain", str(b)]) == 2
    capsys.readouterr()
    assert cli.run(["new", "concept", "ctl-bad", "--title", "bad\x00title", "--brain", str(b)]) == 2
    capsys.readouterr()

    # `bf status --check --fields problems` still exits 1 when `--check` fails despite omitting `ok`.
    (b / "bf.yaml").write_text(
        f"version: {FORMAT}\nname: f15\nloops:\n  miss:\n    run: [echo]\n    requires: [bf-no-such-bin-xyz]\n"
    )
    assert cli.run(["status", "--check", "--fields", "problems", "--brain", str(b)]) == 1
    st_out = json.loads(capsys.readouterr().out)
    assert "ok" not in st_out
    assert st_out["problems"]

    # `bf review --format jsonl|tsv` emits sanitized stderr diagnostic on broken notes while keeping stdout pure.
    (b / "concepts" / "broken.md").write_text("---\n: [broken\n---\n# Bad\n")
    for fmt in ("jsonl", "tsv"):
        assert cli.run(["review", "--format", fmt, "--brain", str(b)]) == 1
        cap = capsys.readouterr()
        assert cap.err.startswith("bf: ")
        assert "broken.md" in cap.err
    (b / "concepts" / "broken.md").unlink()

    # Subcommand `--help` includes descriptive flag help text.
    with pytest.raises(SystemExit) as help_exc:
        cli.run(["read", "--help"])
    assert help_exc.value.code == 0
    help_txt = capsys.readouterr().out
    assert "1-based line slice" in help_txt
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
