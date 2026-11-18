"""End-to-end tests for runnable examples (`context-hub`, `brain`, 5 stdlib `sensors/*`, and `benchmark_scale.py`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bf import cli, engine, jobs, query, records, watch
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def test_context_hub_walkthrough(tmp_path: Path) -> None:
    hub = tmp_path / "context-hub"
    shutil.copytree(ROOT / "examples" / "context-hub", hub)
    demo = hub / "sensors" / "demo.py"
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    store = Store(hub)

    for source in ("workspace", "jira", "github", "gcloud"):
        out = subprocess.run(  # noqa: S603
            [sys.executable, str(demo), source, "2026-09-26T12:00:00Z"],
            cwd=hub,
            capture_output=True,
            timeout=30,
            check=True,
        )
        res = records.ingest(store, source, out.stdout, mode="snapshot", now=now)
        assert res["records"] == 1

    built = engine.build(store)
    assert (built["notes"], built["problems"]) == (1, [])
    val = engine.validate(store)
    assert val["ok"], val["problems"]
    assert query.evaluate(store)["ok"]

    store.write(
        "projects/new-website.md",
        b"---\n"
        b"type: project\n"
        b"status: draft\n"
        b"updated: 2026-09-27\n"
        b"tags: [launch, web]\n"
        b"summary: Launch a product website that explains the product before signup.\n"
        b"---\n\n"
        b"# New website\n\n"
        b"## Decision {#decision owner=product}\n\n"
        b"Start with a single product page. Evidence: [Workspace brief](../memories/workspace/29a8825bd242f14386ee528d76e0e8f1e38f3c8c4047d7b2d6df7493368a17d0.json){rel=cites}.\n"
        b"{: #single-page confidence=high}\n\n"
        b"## Launch review {#launch-review gate=accessibility}\n\n"
        b"Hold public launch: [implementation](../memories/github/da31012c40330e7e21538e7dd57503b16e8a0839159e96137090cccc9910b171.json){rel=implements} and "
        b"[preview](../memories/gcloud/aee50b18a03c89e4883a5acadf5c8614dd7fd2f111c3b616e1a8d6427edc3e3f.json){rel=verifies} are ready, but [review](../memories/jira/c97ace4c8fef2cee8fa0f3c9f52aab18dbd4f42438afe362ffb8f75ce4c04b84.json){rel=blocks} blocks.\n\n"
        b"## Next actions\n\n"
        b"- [ ] Run the keyboard navigation check.\n"
        b"  {: #task-a11y priority=p1}\n",
    )
    assert engine.build(store)["problems"] == []
    assert engine.validate(store)["ok"]
    rev_before = engine.review(store, now=now)
    assert rev_before["stale"] == []
    assert rev_before["tasks_open"] == 1

    jq_res = query.query_jq(store, ".source", limit=20)
    assert dict(Counter(jq_res["items"])) == {"gcloud": 1, "github": 1, "jira": 1, "projects": 1, "workspace": 1}

    shutil.copy2(hub / "fixtures" / "jira-done.json", hub / "fixtures" / "jira.json")
    done_out = subprocess.run(  # noqa: S603
        [sys.executable, str(demo), "jira", "2026-09-28T10:00:00Z"],
        cwd=hub,
        capture_output=True,
        timeout=30,
        check=True,
    )
    records.ingest(store, "jira", done_out.stdout, mode="snapshot", now=now)
    rev_after = engine.review(store, now=now)
    assert rev_after["stale"] == [
        {
            "ref": "projects/new-website.md",
            "title": "New website",
            "reasons": ["newer_evidence"],
            "newer": ["jira:review"],
        }
    ]


def test_example_brain_and_scale_benchmark(tmp_path: Path) -> None:
    b_root = tmp_path / "example"
    shutil.copytree(ROOT / "examples" / "brain", b_root, symlinks=True)
    assert not (b_root / "skills" / "weekly-review").exists()
    assert (b_root / "skills" / "bf" / "SKILL.md").read_bytes() == (ROOT / "src/bf/skills/bf/SKILL.md").read_bytes()
    assert not (b_root / ".agents").exists()

    demo_out = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "sensors" / "demo.py"), "2026-09-18T00:00:00Z", "2026-09-19T00:00:00Z"],
        cwd=b_root,
        capture_output=True,
        timeout=30,
        check=True,
    )
    store = Store(b_root)
    records.ingest(store, "demo", demo_out.stdout, mode="snapshot")
    assert engine.build(store)["problems"] == []
    val = engine.validate(store)
    assert val["ok"], val["problems"]
    assert query.evaluate(store)["ok"]
    rev = engine.review(store, now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    assert len(rev["actions"]) == 2
    edges = query.query_jq(
        store,
        'select(.kind == "project" or .kind == "concept" or .kind == "action") | .links[]? | select(.meta.rel != null)',
        limit=50,
    )
    assert edges["count"] >= 2

    # Exercise `routines/weekly-review.py` (`action:`), `services/local.py` (`send` + `listen --once`),
    # and reactive `jobs.assistant` (`input: prompts/digest.md` on stdin + `{{context}}` JSON)!
    wr = jobs.run_jobs(store, ("weekly-review",))
    assert wr["ok"], wr
    assert engine.validate(store)["ok"]
    local_state = b_root / ".bf" / "local"
    send_res = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "services" / "local.py"), "send"],
        cwd=b_root,
        input=json.dumps({"id": "out-0", "text": "Retention review ready"}) + "\n",
        env={**os.environ, "BF_STATE_DIR": str(local_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(send_res.stdout)["id"] == "out-0"
    assert oct(local_state.stat().st_mode & 0o777) == "0o700"
    assert oct((local_state / "outbox.jsonl").stat().st_mode & 0o777) == "0o600"
    assert "Retention review ready" in (local_state / "outbox.jsonl").read_text()

    # Refuses symlinked outbox.jsonl cleanly!
    sym_state = tmp_path / "sym-local"
    sym_state.mkdir()
    (sym_state / "outbox.jsonl").symlink_to(tmp_path / "target.jsonl")
    sym_send = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "services" / "local.py"), "send"],
        input='{"text": "hi"}\n',
        env={**os.environ, "BF_STATE_DIR": str(sym_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert sym_send.returncode != 0
    assert "refusing" in sym_send.stderr

    (local_state / "inbox.offset").write_text("99\n")
    (local_state / "inbox.jsonl").write_text("not-json\n" + json.dumps({"id": "in-1", "text": "status?"}) + "\n")
    listen = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "services" / "local.py"), "listen", "--once"],
        cwd=b_root,
        env={**os.environ, "BF_STATE_DIR": str(local_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    records.append_event(store, "service.message", "local", json.loads(listen.stdout.strip()))
    (b_root / "routines" / "fake_harness.py").write_text(
        "import json, pathlib, subprocess, sys\n"
        "ctx = json.loads(pathlib.Path(sys.argv[1].removeprefix('Context file: ')).read_text())\n"
        "prompt_stdin = sys.stdin.read()\n"
        "assert 'Digest prompt' in prompt_stdin and pathlib.Path(ctx['input']).name == 'digest.md'\n"
        "msg = {'text': 'Digest from ' + pathlib.Path(ctx['input']).name}\n"
        f"subprocess.run([{sys.executable!r}, 'services/local.py', 'send'], input=json.dumps(msg).encode(), check=True)\n"
    )
    bf_txt = store.read("bf.yaml").decode()
    bf_txt = bf_txt.replace(
        'run: [claude, -p, "Context file: {{context}}"]',
        f'run: [{sys.executable!r}, routines/fake_harness.py, "Context file: {{{{context}}}}"]',
    )
    bf_txt = bf_txt.replace("source: local\n    enabled: false", "source: local\n    enabled: true")
    store.write("bf.yaml", bf_txt.encode())
    tick_res = watch.tick(store)
    assert tick_res["jobs_ran"] == 3
    assert tick_res["ok"]
    assert "Digest from digest.md" in (local_state / "outbox.jsonl").read_text()
    assert engine.validate(store)["ok"]

    bench = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "benchmark_scale.py"),
            "--dir",
            str(tmp_path / "bench"),
            "--records",
            "20",
            "--notes",
            "4",
            "--batch",
            "5",
            "--repeats",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    summary = json.loads(bench.stdout)
    assert (summary["corpus"]["records"], summary["corpus"]["notes"]) == (20, 4)
    assert summary["notes_json_bytes"] > 0


def test_five_example_sensors_emit_schema_valid_records_and_fail_closed(tmp_path: Path, provider: Any) -> None:
    sensor_scripts = sorted(p.name for p in (ROOT / "examples" / "sensors").glob("*.py"))
    assert sensor_scripts == [
        "agent-telemetry.py",
        "git-history.py",
        "github-issues.py",
        "gmail-headers.py",
        "local-documents.py",
    ]

    brain_dir = tmp_path / "sensor-brain"
    assert cli.run(["init", str(brain_dir), "--name", "sensors"]) == 0
    assert (brain_dir / ".agents" / "skills" / "bf").is_symlink()
    assert (brain_dir / ".agents" / "skills" / "bf" / "SKILL.md").read_bytes() == (
        ROOT / "src/bf/skills/bf/SKILL.md"
    ).read_bytes()
    assert "/.agents/skills" in (brain_dir / ".gitignore").read_text().splitlines()
    store = Store(brain_dir)

    # 1. agent-telemetry.py (circular harness telemetry sensor: JSON + JSONL, token redaction, symlink refusal)
    tel_dir = tmp_path / "telemetry"
    tel_dir.mkdir()
    session_file = tel_dir / "session-1.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "role": "user",
                "content": (
                    r'Refactor {"api_key": "quoted-secret-val"} {\"secret\": \"escaped-secret-val\"} '
                    "Bearer secret-token-12345 ya29.a0AfH6SMC_tok\n"
                    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA_unclosed_pem\n" + ("x" * 20000)
                ),
                "timestamp": "2026-11-12T10:00:00Z",
            }
        )
        + "\n"
    )
    ts_epoch = datetime(2026, 11, 12, 10, 0, tzinfo=UTC).timestamp()
    os.utime(session_file, (ts_epoch, ts_epoch))
    # Stale session touched inside window and boundary event at exact END must both be excluded by `[START, END)`!
    stale_file = tel_dir / "stale.jsonl"
    stale_file.write_text(
        json.dumps({"role": "user", "content": "Old turn", "timestamp": "2026-10-15T10:00:00Z"}) + "\n"
    )
    os.utime(stale_file, (ts_epoch, ts_epoch))
    end_file = tel_dir / "at-end.jsonl"
    end_file.write_text(
        json.dumps({"role": "user", "content": "Boundary turn", "timestamp": "2026-11-15T00:00:00Z"}) + "\n"
    )
    os.utime(end_file, (ts_epoch, ts_epoch))

    at = provider.run("agent-telemetry.py", "claude", str(tel_dir), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert at.returncode == 0, at.stderr
    for secret in (
        "quoted-secret-val",
        "escaped-secret-val",
        "secret-token-12345",
        "ya29.",
        "BEGIN RSA PRIVATE KEY",
        "MIIEowIBAAKCAQEA",
    ):
        assert secret not in at.stdout
    assert "[REDACTED]" in at.stdout
    assert "Old turn" not in at.stdout
    assert "Boundary turn" not in at.stdout
    assert records.ingest(store, "agent-telemetry", at.stdout.encode())["added"] == 1

    bad_json_dir = tmp_path / "bad-telemetry"
    bad_json_dir.mkdir()
    bad_jf = bad_json_dir / "corrupt.jsonl"
    bad_jf.write_text("{broken json with top-secret-prompt-content\n")
    os.utime(bad_jf, (ts_epoch, ts_epoch))
    at_corrupt = provider.run(
        "agent-telemetry.py", "claude", str(bad_json_dir), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z"
    )
    assert at_corrupt.returncode != 0
    assert "top-secret-prompt-content" not in at_corrupt.stderr

    (tel_dir / "evil.jsonl").symlink_to(session_file)
    at_fail = provider.run("agent-telemetry.py", "claude", str(tel_dir), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert at_fail.returncode != 0
    assert at_fail.stdout == ""

    # 2. local-documents.py (including spaces in subdirectories and filenames)
    docs_dir = tmp_path / "docs"
    (docs_dir / "Project Notes").mkdir(parents=True)
    (docs_dir / "Project Notes" / "q4 plan.md").write_text("# Design Note\n\nKeep formats simple.\n")
    ld = provider.run("local-documents.py", "personal", str(docs_dir))
    assert ld.returncode == 0, ld.stderr
    assert records.ingest(store, "local-docs", ld.stdout.encode(), mode="snapshot")["added"] == 1

    # 3. github-issues.py (success + failure)
    provider.install(
        "gh",
        [
            {
                "match": ["issue", "list"],
                "stdout": [
                    {
                        "number": 7,
                        "title": "Support attr_list",
                        "body": "Use Python-Markdown.",
                        "state": "OPEN",
                        "url": "https://github.com/acme/demo/issues/7",
                        "author": {"login": "alice"},
                        "assignees": [{"login": "bob"}],
                        "labels": [{"name": "enhancement"}],
                        "createdAt": "2026-11-10T09:00:00Z",
                        "updatedAt": "2026-11-11T10:00:00Z",
                    }
                ],
            },
            {"match": ["issue", "list"], "code": 1, "stderr": "rate limit\n"},
        ],
    )
    ghi = provider.run("github-issues.py", "acme/demo", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghi.returncode == 0, ghi.stderr
    assert records.ingest(store, "github-issues", ghi.stdout.encode())["added"] == 1
    ghi_fail = provider.run("github-issues.py", "acme/demo", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghi_fail.returncode != 0
    assert ghi_fail.stdout == ""

    # 4. gmail-headers.py via fake gws
    ts_ms = str(int(datetime(2026, 11, 12, 10, 0, tzinfo=UTC).timestamp() * 1000))
    provider.install(
        "gws",
        [
            {"match": ["gmail", "users", "messages", "list"], "stdout": {"messages": [{"id": "1931f0a2b3c4d5e6"}]}},
            {
                "match": ["gmail", "users", "messages", "get"],
                "stdout": {
                    "id": "1931f0a2b3c4d5e6",
                    "threadId": "1931f0a2b3c4d5e0",
                    "labelIds": ["INBOX"],
                    "internalDate": ts_ms,
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Weekly review"},
                            {"name": "From", "value": "Alice <alice@example.com>"},
                            {"name": "To", "value": "bob@example.com"},
                            {"name": "Message-ID", "value": "<m1@example.com>"},
                        ]
                    },
                },
            },
            {"match": ["gmail"], "code": 1, "stderr": "gmail error\n"},
        ],
    )
    gm = provider.run("gmail-headers.py", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert gm.returncode == 0, gm.stderr
    assert records.ingest(store, "gmail", gm.stdout.encode())["added"] == 1
    assert provider.run("gmail-headers.py", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").returncode != 0

    # 5. git-history.py on a disposable local Git repository (including folder names with spaces + gitfile confinement)
    git_bin = shutil.which("git")
    assert git_bin is not None
    repos_root = tmp_path / "repos"
    repo = repos_root / "demo repo"
    repo.mkdir(parents=True)
    subprocess.run([git_bin, "-C", str(repo), "init", "-q", "-b", "main"], check=True, timeout=30)  # noqa: S603
    (repo / "README.md").write_text("# Demo\n")
    subprocess.run([git_bin, "-C", str(repo), "add", "README.md"], check=True, timeout=30)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            git_bin,
            "-C",
            str(repo),
            "-c",
            "user.name=Alice",
            "-c",
            "user.email=alice@example.com",
            "commit",
            "-q",
            "-m",
            "feat: initial commit",
        ],
        env={
            "GIT_AUTHOR_DATE": "2026-11-12T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-11-12T10:00:00Z",
            "PATH": "/usr/bin:/bin",
        },
        check=True,
        timeout=30,
    )
    ghist = provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghist.returncode == 0, ghist.stderr
    assert records.ingest(store, "git-history", ghist.stdout.encode())["added"] == 1
    assert provider.run("git-history.py", str(repos_root), "bad-start", "bad-end").returncode != 0

    escape_repo = repos_root / "escape-wt"
    escape_repo.mkdir()
    (escape_repo / ".git").write_text(f"gitdir: {tmp_path}\n")
    ghist_esc = provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghist_esc.returncode != 0
    assert ghist_esc.stdout == ""
    assert "escapes ROOT" in ghist_esc.stderr
    shutil.rmtree(escape_repo)

    # `.git` directory with `commondir` or symlinked `refs` pointing outside ROOT is also refused!
    cd_repo = repos_root / "escape-commondir"
    (cd_repo / ".git").mkdir(parents=True)
    (cd_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (cd_repo / ".git" / "commondir").write_text(f"{tmp_path}\n")
    ghist_cd = provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghist_cd.returncode != 0
    assert "escapes ROOT" in ghist_cd.stderr
    shutil.rmtree(cd_repo)

    sym_git_repo = repos_root / "escape-symref"
    (sym_git_repo / ".git").mkdir(parents=True)
    (sym_git_repo / ".git" / "refs").symlink_to(tmp_path)
    ghist_sym = provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghist_sym.returncode != 0
    assert "symlinked git entry" in ghist_sym.stderr

    assert engine.validate(store)["ok"]
