"""Tests for `bf read`, `bf query` (`rg`, `jq`, sandboxed `duckdb`), and `bf eval`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from bf import engine, query, records
from bf.models import FORMAT, Error, InputError
from bf.storage import Store


def test_read_target_urls_anchors_jq_fragments_cross_brain_and_aliases(tmp_path: Path, brain: Store) -> None:
    overview = query.read_target(brain, "")
    assert overview["kind"] == "overview"
    assert overview["counts"]["projects"] >= 1

    # 1. Markdown note section slicing by `#anchor` and block `#id` fallback.
    sec = query.read_target(brain, "bf:///projects/offline.md?rel=cites#decision")
    assert sec["kind"] == "section"
    assert "## Decision" in sec["text"]
    assert "## Next actions" not in sec["text"]

    blk = query.read_target(brain, "projects/offline#summary")
    assert blk["element"]["id"] == "summary"

    # Task `#id` and Link `#id` anchors resolve cleanly in `bf read`!
    brain.write(
        "concepts/anchors.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Anchors\n\n"
        b"Cite [doc](../memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json){#cite-1 rel=cites}.\n\n"
        b"- [ ] Verify anchors. {: #task-v1 due=2026-12-01}\n",
    )
    engine.build(brain)
    task_read = query.read_target(brain, "concepts/anchors.md#task-v1")
    assert task_read["task"]["id"] == "task-v1"
    link_read = query.read_target(brain, "concepts/anchors.md#cite-1")
    assert link_read["meta"] == {"rel": "cites"}

    note_jq = query.read_target(brain, "bf:///projects/offline.md#.title")
    assert note_jq["jq"] == ["Offline retrieval"]

    non_okf = query.read_target(brain, "docs/index.md")
    assert non_okf["kind"] == "file"

    with pytest.raises(Error, match="missing anchor #nope"):
        query.read_target(brain, "projects/offline.md#nope")

    # 2. JSON file read + `#<jq_filter>` fragment!
    jq_read = query.read_target(brain, "bf:///memories/projects/offline.json#{ref, decision: .attrs.decision}")
    assert jq_read["result"] == [{"ref": "projects/offline.md", "decision": {"owner": "alice"}}]

    plain_json = query.read_target(brain, "memories/projects/offline.json")
    assert plain_json["record"]["ref"] == "projects/offline.md"

    # Sensor memory JSON file read via both `bf:///memories/<source>/<sha256>.json` and relative path + `#<jq>`!
    sensor_mem = "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
    sensor_json = query.read_target(brain, f"bf:///{sensor_mem}")
    assert sensor_json["ref"] == "meetings:decision-1"
    assert sensor_json["kind"] == "memory"
    assert sensor_json["record"]["title"] == "Preserve durable evidence"
    sensor_jq = query.read_target(brain, f"{sensor_mem}#.title")
    assert sensor_jq == {
        "brain": "fixture",
        "kind": "jq",
        "ref": "meetings:decision-1",
        "result": ["Preserve durable evidence"],
    }
    with pytest.raises(Error, match=r"record memories/meetings/0000\.json not found"):
        query.read_target(brain, "bf:///memories/meetings/0000.json")

    # 3. Sensor record by `source:id` + jq fragment + alias lookup!
    rec = query.read_target(brain, "meetings:decision-1#.title")
    assert rec["result"] == ["Preserve durable evidence"]

    records.ingest(brain, "repos", b'[{"id":"r1","title":"Brain Repo","aliases":["repo:fmind/bf"]}]')
    by_alias = query.read_target(brain, "repo:fmind/bf")
    assert by_alias["ref"] == "repos:r1"

    # 4. Cross-brain `bf://sib/...` read!
    sib_root = tmp_path / "sib"
    engine.init_brain(sib_root, name="sib")
    sib = Store(sib_root)
    sib.write(
        "concepts/topic.md", b"---\ntype: guide\nstatus: stable\n---\n# Sib Topic\n\n## Core {#core}\n\nHello sib.\n"
    )
    engine.build(sib)
    brain.write("bf.yaml", f"version: {FORMAT}\nname: fixture\nbrains:\n  sib: {sib_root}\n".encode())

    cross = query.read_target(brain, "bf://sib/concepts/topic#core")
    assert cross["brain"] == "sib"
    assert "Hello sib." in cross["text"]

    # Cross-brain percent-encoded paths (`%25`) and jq fragments with spaces/quotes!
    sib.write("docs/100%25.md", b"# Literal Percent\n")
    cross_pct = query.read_target(brain, "bf://sib/docs/100%2525.md")
    assert cross_pct["brain"] == "sib"
    assert "Literal Percent" in cross_pct["text"]
    cross_jq = query.read_target(brain, 'bf://sib/memories/concepts/topic.json#select(.status == "stable") | .title')
    assert cross_jq["result"] == ["Sib Topic"]

    # Overview includes `attention` when a configured job/channel/server is in `failed` state!
    brain.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: fixture\nbrains:\n  sib: {sib_root}\n"
        "jobs:\n  broken:\n    run: [echo, hi]\n"
        "channels:\n  chat:\n    send: [echo, hi]\n".encode(),
    )
    brain.write(
        "logs/broken.json",
        b'{"kind":"job","name":"broken","status":"failed","updated":"2026-11-15T10:00:00Z","history":[{"run":"r1","status":"failed","started":"2026-11-15T10:00:00Z","ended":"2026-11-15T10:00:01Z","duration_ms":1000,"exit_code":1,"error":"boom"}]}\n',
    )
    brain.write(
        "logs/chat.json",
        b'{"kind":"channel","name":"chat","status":"failed","updated":"2026-11-15T10:00:00Z","history":[]}\n',
    )
    ov_att = query.read_target(brain, "")
    assert ov_att["attention"] == [
        {"kind": "job", "name": "broken", "failed": True},
        {"kind": "channel", "name": "chat", "failed": True},
    ]

    for bad, exc in (
        ("https://example.com", InputError),
        ("bf://bad/no-such.md", Error),
        ("concepts/missing.md", Error),
        ("meetings:missing", Error),
    ):
        with pytest.raises(exc):
            query.read_target(brain, bad)


def test_query_rg_jq_and_duckdb_sandboxing(tmp_path: Path, brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    rg_hits = query.search_rg(brain, "durable", limit=1)
    assert rg_hits["engine"] == "rg"
    assert len(rg_hits["items"]) == 1

    # Match-centered `rg` snippets on compact single-line JSON records (> 240 chars, UTF-8 safe)!
    long_body = ("Café — " * 50) + "deep-centered-needle" + (" omega" * 50)
    records.ingest(brain, "docs", json.dumps([{"id": "d-long", "title": "EarlyHead", "text": long_body}]).encode())
    deep_hit = query.search_rg(brain, "deep-centered-needle", scope="docs")
    assert deep_hit["count"] == 1
    assert "deep-centered-needle" in deep_hit["items"][0]["snippet"]
    assert len(deep_hit["items"][0]["snippet"]) == 240
    head_hit = query.search_rg(brain, "d-long", scope="docs")
    assert head_hit["count"] == 1
    assert head_hit["items"][0]["snippet"].startswith('{"id":"d-long"')
    assert len(head_hit["items"][0]["snippet"]) == 240
    tail_hit = query.search_rg(brain, "EarlyHead", scope="docs")
    assert tail_hit["count"] == 1
    assert "EarlyHead" in tail_hit["items"][0]["snippet"]

    # Scope resolution: OKF note path, explicit memories/*.json path, subfolder, and empty scope!
    assert query.search_rg(brain, "durable", scope="projects/offline.md")["count"] == 1
    assert query.query_jq(brain, ".ref", scope="memories/projects/offline.json")["items"] == ["projects/offline.md"]
    assert query.query_jq(brain, ".ref", scope="concepts")["count"] >= 1
    assert query.search_rg(brain, "durable", scope="empty-source")["items"] == []
    assert query.query_jq(brain, ".ref", scope="empty-source")["items"] == []
    assert query.query_sql(brain, "SELECT count(*) AS n FROM records", scope="empty-source")["items"] == [{"n": 0}]
    with pytest.raises(InputError, match="jq failed"):
        query.query_jq(brain, "[[[", scope="empty-source")

    empty_rg = query.search_rg(brain, "zzz-never-matches-zzz")
    assert empty_rg["items"] == []
    with pytest.raises(InputError, match="rg failed"):
        query.search_rg(brain, "[unclosed")

    jq_hits = query.query_jq(brain, 'select(.kind == "project") | .ref', limit=1)
    assert jq_hits["items"] == ["projects/offline.md"]

    with pytest.raises(Error, match="jq failed"):
        query.query_jq(brain, "invalid syntax [[[")
    assert query.check_jq_syntax("") is not None
    assert query.check_jq_syntax("[[[") is not None

    # Sandboxed DuckDB query over materialized `records` temp table + locked `512MB` memory_limit BEFORE read_json!
    captured_scripts: list[str] = []
    real_run = query.subprocess.run

    def spy_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if "-c" in cmd:
            captured_scripts.append(cmd[cmd.index("-c") + 1])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(query.subprocess, "run", spy_run)
    sql_hits = query.query_sql(brain, "SELECT ref, kind FROM records WHERE kind = 'project'")
    monkeypatch.setattr(query.subprocess, "run", real_run)
    assert sql_hits["items"] == [{"ref": "projects/offline.md", "kind": "project"}]
    assert (
        captured_scripts[0].index("SET memory_limit = '512MB';")
        < captured_scripts[0].index("CREATE TEMP TABLE records")
        < captured_scripts[0].index("SET lock_configuration = true;")
    )
    mem_cfg = query.query_sql(brain, "SELECT current_setting('memory_limit') AS lim")["items"][0]["lim"]
    assert "MiB" in mem_cfg or "MB" in mem_cfg
    with pytest.raises(InputError, match="duckdb failed"):
        query.query_sql(brain, "SET memory_limit = '2GB'")
    assert query.query_sql(brain, "SELECT ref FROM records", scope="memories/projects/offline.json")["count"] == 1
    with pytest.raises(InputError, match="duckdb failed"):
        query.query_sql(brain, "SELECT * FROM nonexistent_table")
    with pytest.raises(InputError, match="single SQL query"):
        query.query_sql(brain, "SELECT 1 AS a; SELECT 2 AS b")

    # Brain nested inside an ancestor directory named `memories` still extracts true source names!
    nested_root = tmp_path / "memories" / "nested-brain"
    engine.init_brain(nested_root, name="nested")
    nested_store = Store(nested_root)
    nested_sql = query.query_sql(nested_store, "SELECT DISTINCT source FROM records")
    assert nested_sql["items"] == [{"source": "concepts"}]

    # Security: dot-commands (`.shell`) and filesystem exfiltration (`COPY ... TO` / `read_text`) MUST fail closed!
    with pytest.raises(InputError, match="dot-commands"):
        query.query_sql(brain, ".shell id")

    exfil = tmp_path / "stolen.json"
    with pytest.raises(Error, match="duckdb failed"):
        query.query_sql(brain, f"COPY records TO '{exfil}'")
    assert not exfil.exists()

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET")
    with pytest.raises(Error, match="duckdb failed"):
        query.query_sql(brain, f"SELECT * FROM read_csv_auto('{secret}')")  # noqa: S608 - sandbox exfiltration test

    for bad_fn in (
        lambda: query.search_rg(brain, ""),
        lambda: query.search_rg(brain, "x", limit=0),
        lambda: query.query_jq(brain, ""),
        lambda: query.query_jq(brain, ".", limit=0),
        lambda: query.query_sql(brain, ""),
    ):
        with pytest.raises(InputError):
            bad_fn()

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert query.find_by_alias(brain, "meetings:nonexistent") is None
    with pytest.raises(Error, match="is required on PATH"):
        query.search_rg(brain, "durable")


def test_evaluate_passes_and_reports_failures(brain: Store) -> None:
    brain.write(
        "evals/suite.json",
        json.dumps(
            {
                "version": FORMAT,
                "cases": [
                    {
                        "name": "rg-offline",
                        "engine": "rg",
                        "query": "durable",
                        "expect_refs": ["projects/offline.md"],
                        "forbid_refs": ["meetings:lunch"],
                        "min_count": 1,
                    },
                    {
                        "name": "jq-project",
                        "engine": "jq",
                        "query": 'select(.kind == "project") | {ref}',
                        "expect_equals": [{"ref": "projects/offline.md"}],
                    },
                    {
                        "name": "sql-count",
                        "engine": "sql",
                        "query": "SELECT ref FROM records WHERE kind = 'concept'",
                        "max_count": 5,
                    },
                    {
                        "name": "read-decision",
                        "engine": "read",
                        "query": "bf:///projects/offline.md#decision",
                        "expect_text": ["retention"],
                    },
                ],
            }
        ).encode(),
    )
    res = query.evaluate(brain)
    assert res["ok"], res

    # Failing eval file reports structured failure reasons without raising.
    brain.write(
        "evals/failing.yaml",
        f"version: {FORMAT}\n"
        "cases:\n"
        "  - name: bad-case\n"
        "    engine: rg\n"
        "    query: durable\n"
        "    expect_refs: [concepts/missing.md]\n"
        "    forbid_refs: [projects/offline.md]\n"
        "    min_count: 99\n"
        "    max_count: 0\n"
        "  - name: broken-read\n"
        "    engine: read\n"
        "    query: concepts/nonexistent.md\n".encode(),
    )
    brain.write("evals/broken.yaml", b"version: 99\ncases: []\n")
    brain.write("evals/badschema.yaml", f"version: {FORMAT}\ncases: not-a-list\n".encode())
    brain.write("evals/scalar.json", b"123\n")
    fail_res = query.evaluate(brain, target="failing")
    assert not fail_res["ok"]
    assert fail_res["failed"] == 2
    assert len(fail_res["cases"][0]["reasons"]) == 4

    all_res = query.evaluate(brain)
    assert not all_res["ok"]
    assert len(all_res["problems"]) >= 3

    with pytest.raises(InputError, match="no eval file matching"):
        query.evaluate(brain, target="nonexistent-suite")


@pytest.mark.skipif(shutil.which("duckdb") is None, reason="duckdb binary required")
def test_query_sql_isolates_duckdb_temp_directory_from_cwd(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "caller-cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    res = query.query_sql(brain, "SELECT i FROM range(20000) AS t(i) ORDER BY i DESC LIMIT 3")
    assert [row["i"] for row in res["items"]] == [19999, 19998, 19997]
    assert list(workdir.iterdir()) == []
    assert not (brain.root / ".tmp").exists()
