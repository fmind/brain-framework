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
from bf.models import MAX_BATCH
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
    assert "pass either positional QUERY" in capsys.readouterr().err
    assert cli.run(["query", "pat", "--jq", ".", "--brain", str(target)]) == 2
    capsys.readouterr()
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

    # `mcp` rejects `--port` with stdio transport (exit 2) and accepts `--transport http --port`.
    called_mcp: list[tuple[str, str, int | None]] = []
    monkeypatch.setattr(
        "bf.mcp.serve", lambda b, *, transport="stdio", port=None: called_mcp.append((b, transport, port))
    )
    assert cli.run(["mcp", "--brain", str(target)]) == 0
    assert cli.run(["mcp", "--brain", str(target), "--transport", "http", "--port", "9000"]) == 0
    assert called_mcp == [(str(target), "stdio", None), (str(target), "http", 9000)]

    assert cli.run(["eval", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["status", "--check", "--brain", str(target)]) == 0
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["ok"] is True
    assert st_out["counts"]["actions"] == 1
    assert st_out["attention"] == []

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
    monkeypatch.setattr(engine, "review", lambda _store: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.run(["review", "--brain", str(target)]) == 130


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
