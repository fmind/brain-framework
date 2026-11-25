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

from bf import cli, engine, loops, query, records, state
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
    eval_env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run([sys.executable, str(hub / "evals" / "smoke.py")], cwd=hub, env=eval_env, check=True, timeout=30)  # noqa: S603

    store.write(
        "projects/new-website.md",
        b"---\n"
        b"type: project\n"
        b"status: active\n"
        b"created: 2026-09-22\n"
        b"updated: 2026-09-27\n"
        b"tags: [launch, web]\n"
        b"description: Launch a product website that explains the product before signup.\n"
        b"---\n\n"
        b"# New website\n\n"
        b"## Decision {#decision owner=product}\n\n"
        b"Start with a single product page. Evidence: [Workspace brief](workspace:brief){rel=cites}.\n\n"
        b"## Launch review {#launch-review gate=accessibility}\n\n"
        b"Hold public launch: [implementation](github:implementation){rel=implements} and "
        b"[preview](gcloud:deployment){rel=verifies} are ready, but [review](jira:review){rel=blocks} blocks.\n\n"
        b"## Next actions\n\n"
        b"- [ ] Run the keyboard navigation check.\n"
        b"  {: priority=p1}\n",
    )
    assert engine.build(store)["problems"] == []
    assert engine.validate(store)["ok"]
    rev_before = engine.review(store, now=now)
    assert rev_before["stale"] == []
    assert rev_before["total_tasks_open"] == 1

    all_res = query.query(store, None, kind="memory", limit=20)
    assert dict(Counter(it["source"] for it in all_res["items"])) == {
        "gcloud": 1,
        "github": 1,
        "jira": 1,
        "workspace": 1,
    }
    assert query.query(store, "keyboard navigation")["engine"] == "fts5"

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
    assert not (b_root / "skills").exists()
    assert not (b_root / "loops").exists()
    assert not (b_root / ".agents").exists()

    demo_out = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "sensors" / "demo.py")],
        cwd=b_root,
        env={**os.environ, "BF_START": "2026-09-18T00:00:00Z", "BF_END": "2026-09-19T00:00:00Z"},
        capture_output=True,
        timeout=30,
        check=True,
    )
    store = Store(b_root)
    records.ingest(store, "demo", demo_out.stdout, mode="snapshot")
    assert engine.build(store)["problems"] == []
    val = engine.validate(store)
    assert val["ok"], val["problems"]
    eval_env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}
    subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "evals" / "smoke.py")], cwd=b_root, env=eval_env, check=True, timeout=30
    )

    now_ex = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    wr = loops.run_loops(store, ("weekly-review",), now=now_ex)
    assert wr["ok"], wr
    assert wr["results"][0]["summary"]["records"] == 1
    ud = loops.run_loops(store, ("usage-demo",), now=now_ex)
    assert ud["ok"], ud
    assert query.query(store, None, rtype="session", scope="usage-demo")["count"] == 1
    assert engine.validate(store)["ok"]

    local_state = b_root / ".bf" / "state" / "local"
    send_res = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "routines" / "local.py"), "send"],
        cwd=b_root,
        input=json.dumps({"id": "out-0", "text": "Retention review ready"}) + "\n",
        env={**os.environ, "BF_STATE": str(local_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(send_res.stdout)["id"] == "out-0"
    assert oct(local_state.stat().st_mode & 0o777) == "0o700"
    assert oct((local_state / "outbox.jsonl").stat().st_mode & 0o777) == "0o600"
    assert "Retention review ready" in (local_state / "outbox.jsonl").read_text()

    sym_state = tmp_path / "sym-local"
    sym_state.mkdir(mode=0o700)
    (sym_state / "outbox.jsonl").symlink_to(tmp_path / "target.jsonl")
    sym_send = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "routines" / "local.py"), "send"],
        input='{"text": "hi"}\n',
        env={**os.environ, "BF_STATE": str(sym_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert sym_send.returncode != 0
    assert "refusing" in sym_send.stderr

    loose_state = tmp_path / "loose-local"
    loose_state.mkdir()
    loose_state.chmod(0o755)
    loose_send = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "routines" / "local.py"), "send"],
        input='{"text": "hi"}\n',
        env={**os.environ, "BF_STATE": str(loose_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert loose_send.returncode != 0
    assert "mode 700" in loose_send.stderr

    (local_state / "inbox.offset").write_text("99\n")
    (local_state / "inbox.jsonl").write_text("not-json\n" + json.dumps({"id": "in-1", "text": "status?"}) + "\n")
    listen = subprocess.run(  # noqa: S603
        [sys.executable, str(b_root / "routines" / "local.py"), "listen", "--once"],
        cwd=b_root,
        env={**os.environ, "BF_STATE": str(local_state)},
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    state.append_event(store, "loop.message", "local", json.loads(listen.stdout.strip()))
    bf_txt = store.read("bf.yaml").decode()
    bf_txt = bf_txt.replace(
        "      - event:\n          kind: loop.message\n          source: local\n    enabled: false",
        "      - event:\n          kind: loop.message\n          source: local\n    enabled: true",
    )
    store.write("bf.yaml", bf_txt.encode())
    from bf import config as cfg_m

    ex_cfg = cfg_m.load(store)
    a_argv, a_ing, _, _, a_timeout = cfg_m.resolve_loop(store, ex_cfg.loops["assistant"], ex_cfg.reserved_sources)
    assert a_argv == ("agents/assistant.py",)
    assert a_ing == ""
    assert a_timeout == 900
    due_res = loops.run_loops(store, due_only=True, now=now_ex)
    assert due_res["ran"] == 4
    assert {r["loop"] for r in due_res["results"]} == {"demo", "usage-demo", "weekly-review", "assistant"}
    assert due_res["ok"]
    assert "Digest from digest.md" in (local_state / "outbox.jsonl").read_text()
    assert "assistant.digest" in store.read("logs/assistant.jsonl").decode()
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
    assert summary["notes_jsonl_bytes"] > 0
    assert summary["index_sqlite_bytes"] > 0


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
    assert not (brain_dir / ".agents").exists()
    assert not (brain_dir / "skills").exists()
    store = Store(brain_dir)

    # 1. agent-telemetry.py
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
    tel_rec = json.loads(store.read("memories/agent-telemetry.jsonl").decode().splitlines()[0])
    assert tel_rec["harness"] == "claude"
    assert tel_rec["prompts"] >= 1
    assert tel_rec["session_id"].startswith("claude-")

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

    # 2. local-documents.py (no file:// URL emitted; ingests cleanly with native core validation)
    docs_dir = tmp_path / "docs"
    (docs_dir / "Project Notes").mkdir(parents=True)
    (docs_dir / "Project Notes" / "q4 plan.md").write_text("# Design Note\n\nKeep formats simple.\n")
    ld = provider.run("local-documents.py", "personal", str(docs_dir))
    assert ld.returncode == 0, ld.stderr
    assert "file://" not in ld.stdout
    assert records.ingest(store, "local-docs", ld.stdout.encode(), mode="snapshot")["added"] == 1

    # 3. github-issues.py (rejects leading-dash / dotdot repo names)
    provider.install(
        "gh",
        [
            {
                "match": ["issue", "list"],
                "stdout": [
                    {
                        "number": 7,
                        "title": "Support attr_list (DB_PASSWORD=topsecretpw)",
                        "body": "Use CommonMark. https://:pwonlysecret@git.example.com/repo",
                        "state": "OPEN",
                        "url": "https://github.com/acme/demo/issues/7",
                        "author": {"login": "alice"},
                        "assignees": [{"login": "bob"}],
                        "labels": [{"name": "enhancement"}],
                        "createdAt": "2026-11-10T09:00:00Z",
                        "updatedAt": "2026-11-11T10:00:00Z",
                    },
                    {
                        "number": 8,
                        "title": "Bad URL scheme and non-string URL",
                        "body": "",
                        "state": "OPEN",
                        "url": "javascript:alert(1)",
                        "author": {"login": "alice"},
                        "assignees": [],
                        "labels": [],
                        "createdAt": "2026-11-10T09:00:00Z",
                        "updatedAt": "2026-11-11T10:00:00Z",
                    },
                    {
                        "number": 9,
                        "title": "Non-string URL",
                        "body": "",
                        "state": "OPEN",
                        "url": 12345,
                        "author": {"login": "alice"},
                        "assignees": [],
                        "labels": [],
                        "createdAt": "2026-11-10T09:00:00Z",
                        "updatedAt": "2026-11-11T10:00:00Z",
                    },
                ],
            },
            {"match": ["issue", "list"], "code": 1, "stderr": "rate limit\n"},
        ],
    )
    ghi = provider.run("github-issues.py", "acme/demo", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghi.returncode == 0, ghi.stderr
    assert "topsecretpw" not in ghi.stdout
    assert "pwonlysecret" not in ghi.stdout
    assert "javascript:" not in ghi.stdout
    assert "[REDACTED]" in ghi.stdout
    ghi_recs = json.loads(ghi.stdout)
    assert len(ghi_recs) == 3
    assert "url" not in ghi_recs[1]
    assert "url" not in ghi_recs[2]
    assert records.ingest(store, "github-issues", ghi.stdout.encode())["added"] == 3
    ghi_fail = provider.run("github-issues.py", "acme/demo", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghi_fail.returncode != 0
    assert ghi_fail.stdout == ""
    assert (
        provider.run("github-issues.py", "-evil/repo", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").returncode != 0
    )
    assert (
        provider.run("github-issues.py", "acme/..bad", "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").returncode != 0
    )

    # 4. gmail-headers.py
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

    # 5. git-history.py
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

    wt_dir = repos_root / "demo-wt"
    subprocess.run([git_bin, "-C", str(repo), "worktree", "add", "-b", "wt", str(wt_dir)], check=True, timeout=30)  # noqa: S603
    ghist_wt = provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z")
    assert ghist_wt.returncode == 0, ghist_wt.stderr
    subprocess.run([git_bin, "-C", str(repo), "worktree", "remove", "--force", str(wt_dir)], check=True, timeout=30)  # noqa: S603

    cd_repo = repos_root / "escape-commondir"
    (cd_repo / ".git" / "objects").mkdir(parents=True)
    (cd_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (cd_repo / ".git" / "commondir").write_text(f"{tmp_path}\n")
    assert (
        "commondir target escapes ROOT"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )
    shutil.rmtree(cd_repo)

    sym_git_repo = repos_root / "escape-symref"
    (sym_git_repo / ".git").mkdir(parents=True)
    (sym_git_repo / ".git" / "refs").symlink_to(tmp_path)
    assert (
        "symlinked git entry"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )
    shutil.rmtree(sym_git_repo)

    pack_repo = repos_root / "escape-pack"
    (pack_repo / ".git" / "objects" / "pack").mkdir(parents=True)
    (pack_repo / ".git" / "objects" / "pack" / "evil.pack").symlink_to(tmp_path)
    assert (
        "symlinked git entry"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )
    shutil.rmtree(pack_repo)

    rt_repo = repos_root / "escape-reftable"
    (rt_repo / ".git" / "reftable").mkdir(parents=True)
    (rt_repo / ".git" / "reftable" / "tables.list").symlink_to(tmp_path)
    assert (
        "symlinked git entry"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )
    shutil.rmtree(rt_repo)

    alt_repo = repos_root / "escape-alt"
    (alt_repo / ".git" / "objects" / "info").mkdir(parents=True)
    (alt_repo / ".git" / "objects" / "info" / "alternates").write_text('"/etc"\n')
    assert (
        "quoted git alternates"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )
    (alt_repo / ".git" / "objects" / " #").mkdir()
    (alt_repo / ".git" / "objects" / "info" / "alternates").write_text(" #/../../../../../etc\n")
    assert (
        "escapes ROOT"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )
    (alt_repo / ".git" / "objects" / "info" / "alternates").write_text(("#" * 4100) + "\n/etc\n")
    assert (
        "exceeds 4096 bytes"
        in provider.run("git-history.py", str(repos_root), "2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z").stderr
    )

    # `examples/brain/routines/local.py`: refuses symlinked `.bf` without `BF_BRAIN`, handles empty `inbox.jsonl` truncation.
    local_svc = ROOT / "examples" / "brain" / "routines" / "local.py"
    svc_tmp = tmp_path / "svc-sec"
    svc_tmp.mkdir()
    outside_dir = tmp_path / "svc-outside"
    outside_dir.mkdir(mode=0o755)
    (svc_tmp / ".bf").symlink_to(outside_dir)
    env_no_brain = {k: v for k, v in os.environ.items() if not k.startswith("BF_")}

    def run_local(args: list[str], env: dict[str, str], *, stdin: str = "", check: bool = True):
        cmd = [sys.executable, str(local_svc), *args]
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=30, check=check)  # noqa: S603

    sym_svc = run_local(
        ["send"], {**env_no_brain, "BF_STATE": str(svc_tmp / ".bf" / "local")}, stdin='{"text":"hi"}\n', check=False
    )
    assert sym_svc.returncode != 0
    assert "refusing symlink" in sym_svc.stderr
    assert not (outside_dir / "local").exists()

    trunc_state = tmp_path / "svc-trunc" / ".bf" / "local"
    env_trunc = {**env_no_brain, "BF_STATE": str(trunc_state)}
    (tmp_path / "svc-trunc").mkdir()
    run_local(["listen", "--once"], env_trunc)
    (trunc_state / "inbox.jsonl").write_text('{"id":"m1","text":"a"}\n{"id":"m2","text":"b"}\n')
    assert len(run_local(["listen", "--once"], env_trunc).stdout.splitlines()) == 2
    (trunc_state / "inbox.jsonl").write_text("")
    run_local(["listen", "--once"], env_trunc)
    assert (trunc_state / "inbox.offset").read_text().strip() == "0"
    (trunc_state / "inbox.jsonl").write_text('{"id":"m3","text":"c"}\n{"id":"m4","text":"d"}\n')
    r_after = run_local(["listen", "--once"], env_trunc)
    assert [json.loads(ln)["id"] for ln in r_after.stdout.splitlines()] == ["m3", "m4"]

    assert engine.validate(store)["ok"]
