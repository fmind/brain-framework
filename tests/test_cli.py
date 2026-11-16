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

    # Oversized stdin -> exit 1.
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x" * (MAX_BATCH + 1)), encoding="utf-8"))
    assert cli.run(["ingest", "notes-feed", "--brain", str(target)]) == 1
    assert "exceeds" in capsys.readouterr().err

    # action, read, query, eval, status, watch --once, build, validate, review -> exit 0.
    assert cli.run(["action", "Audit CLI", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True

    assert cli.run(["read", "bf:///concepts/welcome.md#conventions", "--brain", str(target)]) == 0
    assert "attr_list" in json.loads(capsys.readouterr().out)["text"]

    assert cli.run(["query", "Conventions", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] >= 1

    assert cli.run(["query", "--jq", 'select(.kind=="concept") | .ref', "--brain", str(target)]) == 0
    assert "concepts/welcome.md" in json.loads(capsys.readouterr().out)["items"]

    assert cli.run(["query", "--sql", "SELECT count(*) AS n FROM records", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["n"] >= 2

    assert cli.run(["query", "--brain", str(target)]) == 2
    assert "pass a QUERY pattern" in capsys.readouterr().err
    assert cli.run(["query", "pat", "--jq", ".", "--brain", str(target)]) == 2
    capsys.readouterr()
    assert cli.run(["review", "--brain", str(target), "--", "extra"]) == 2
    capsys.readouterr()

    assert cli.run(["run", "--dry-run", "build", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "dry_run"
    assert cli.run(["run", "--hook", "pre-commit", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    (target / "bf.yaml").write_text((target / "bf.yaml").read_text() + "channels:\n  chat:\n    send: [echo, '{}']\n")
    assert cli.run(["post", "hello", "--dry-run", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"

    called_mcp: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        "bf.mcp.serve", lambda b, *, transport="stdio", port=8765: called_mcp.append((b, transport, port))
    )
    assert cli.run(["mcp", "--brain", str(target), "--port", "9000"]) == 0
    assert called_mcp == [(str(target), "stdio", 9000)]

    assert cli.run(["eval", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["status", "--check", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.run(["watch", "--once", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["brain"] == "cli-demo"

    assert cli.run(["build", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == 2

    assert cli.run(["validate", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert cli.run(["review", "--brain", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["brain"] == "cli-demo"

    # Broken note makes `build` and `validate` exit 1 while still emitting compact JSON.
    (target / "concepts" / "broken.md").write_text("---\nid: forbidden\n---\n# Bad\n")
    assert cli.run(["build", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["problems"]
    assert cli.run(["validate", "--brain", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False

    # Invalid flag or reserved source -> exit 2.
    assert cli.run(["--unknown-flag"]) == 2
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
