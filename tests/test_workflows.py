"""Decision workflows retain selected evidence without loading it into model context or mutating brains."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from bf import retrieve
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/bf-learn/scripts/evidence.py"
NOTE = {"brain": "example", "ref": "projects/policy.md#retention", "text": "## Retention\n\nKeep the latest version.\n"}
RECORD: dict = {
    "brain": "example",
    "ref": "mail:policy",
    "record": {"id": "policy", "title": "Policy", "text": "Keep one revision.", "attributes": {"observed": "old"}},
    "external": True,
    "collection": {"state": "active", "freshness": "fresh", "trust": "external"},
}


def execute(mode: str, *values: dict, raw: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed local helper; synthetic input only
        [sys.executable, str(HELPER), mode],
        input=raw or "\n".join(json.dumps(value) for value in values),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def run(mode: str, *values: dict) -> dict:
    result = execute(mode, *values)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_helper_is_a_standalone_script_for_older_interpreters() -> None:
    # Agents invoke it with their own python3; the package itself requires 3.14.
    ast.parse(HELPER.read_text(), feature_version=(3, 11))
    assert HELPER.read_text().startswith("#!/usr/bin/env python3\n")
    assert os.access(HELPER, os.X_OK)


def test_capture_keeps_only_selected_evidence_and_comparison_is_compact() -> None:
    capture = run("capture", {**NOTE, "backlinks": [{"text": "UNRELATED"}], "claims": [{"text": "UNRELATED"}]})
    assert capture["text"] == NOTE["text"]
    assert "UNRELATED" not in json.dumps(capture)
    assert capture["external"] is False
    assert len(capture["sha256"]) == 64
    assert run("compare", capture, NOTE)["state"] == "unchanged"
    changed = run("compare", capture, {**NOTE, "text": "## Retention\n\nKeep selected older versions.\n"})
    assert changed["state"] == "changed"
    assert changed["content_changed"]
    assert "Keep" not in json.dumps(changed)
    assert len(json.dumps(changed)) < 512


def test_only_records_from_owner_sources_are_owner_text() -> None:
    owner = {**RECORD, "collection": {**RECORD["collection"], "trust": "owner"}}
    del owner["external"]
    capture = run("capture", owner)
    assert capture["external"] is False
    assert run("compare", capture, owner)["external"] is False
    # A record without declared trust is external, whatever its reply omits.
    unlabeled = {key: value for key, value in RECORD.items() if key != "external"}
    assert run("capture", {**unlabeled, "collection": {"state": "active", "freshness": "fresh"}})["external"] is True
    assert run("compare", capture, RECORD)["external"] is True


def test_record_observation_does_not_retrigger_but_revision_and_fields_do() -> None:
    capture = run("capture", RECORD)
    observed = {**RECORD, "record": {**RECORD["record"], "attributes": {"observed": "new"}}}
    assert run("compare", capture, observed)["state"] == "unchanged"
    assert run("compare", capture, observed)["external"] is True
    for update in [
        {"fields": {"owner": "person:other"}},
        {"text": "Keep two revisions."},
        {"attributes": {"updated": "new"}},
    ]:
        assert run("compare", capture, {**RECORD, "record": {**RECORD["record"], **update}})["state"] == "changed"


@pytest.mark.parametrize(
    "update",
    [
        {"problems": [{"error": "PRIVATE FAILURE"}]},
        {"stale": ["example"]},
        {"collection": {"state": "active", "freshness": "stale"}},
        {"collection": {"state": "active", "freshness": "unknown"}},
        {"collection": {"state": "active", "freshness": "fresh", "failed": True}},
        {"collection": {"state": "historical", "freshness": "fresh"}},
        {"record": {**RECORD["record"], "attributes": {"partial": True}}},
    ],
)
def test_uncertain_evidence_never_clears_an_intention_or_dependency(update: dict) -> None:
    capture = run("capture", RECORD)
    result = run("compare", capture, {**RECORD, **update})
    assert result["state"] == "unknown"
    assert result["limitations"]
    assert "PRIVATE FAILURE" not in json.dumps(result)


def test_partial_baseline_stays_unknown_and_missing_source_is_not_unchanged() -> None:
    partial = {**RECORD, "record": {**RECORD["record"], "attributes": {"partial": True}}}
    capture = run("capture", partial)
    assert capture["limitations"] == ["partial record"]
    assert run("compare", capture, RECORD)["state"] == "unknown"
    missing = execute("compare", capture)
    assert missing.returncode == 1
    assert not missing.stdout


@pytest.mark.parametrize(
    "reply",
    [
        {**NOTE, "problems": [{"error": "PRIVATE FAILURE"}]},
        {**NOTE, "stale": ["example"]},
        {"page": "projects", **NOTE},
        {**NOTE, "record": RECORD["record"]},
        {**NOTE, "text": []},
        {**NOTE, "brain": "../secret"},
        {**NOTE, "ref": "bad\nref"},
        {**RECORD, "record": {"title": "missing id"}},
        {**RECORD, "record": {**RECORD["record"], "attributes": []}},
        {**RECORD, "collection": []},
    ],
)
def test_bad_captures_fail_without_private_error_text(reply: dict) -> None:
    result = execute("capture", reply)
    assert result.returncode == 1
    assert not result.stdout
    assert "PRIVATE FAILURE" not in result.stderr


def test_bad_json_and_oversized_input_fail_before_output() -> None:
    for data in [
        "PRIVATE FAILURE",
        "[]",
        "{} {}",
        '{"brain":"example","brain":"other"}',
        "[" * 2000,
        " " * ((9 << 20) + 1),
    ]:
        result = execute("capture", raw=data)
        assert result.returncode == 1
        assert not result.stdout
        assert "PRIVATE FAILURE" not in result.stderr
    assert execute("not-a-mode", NOTE).returncode == 2


def test_capture_identity_and_integrity_are_required() -> None:
    capture = run("capture", NOTE)
    for before, after in [
        ({**capture, "text": "altered"}, NOTE),
        (capture, {**NOTE, "brain": "other"}),
        (capture, {**NOTE, "ref": "projects/other.md"}),
        ({**capture, "capture_version": True}, NOTE),
        ({**capture, "captured_at": "2026-09-25"}, NOTE),
    ]:
        result = execute("compare", before, after)
        assert result.returncode == 1
        assert not result.stdout


def test_real_reads_keep_history_after_replacement_and_explain_direct_impact(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 5\nname: fixture\nschema:\n  depends-on:\n    description: Needs review when evidence changes.\n"
        b"    type: identity\n    cardinality: many\n    relation: true\n",
    )
    brain.write("projects/policy.md", b"# Policy\n\n## Retention {#retention}\n\nKeep one version.\n")
    brain.write(
        "projects/decision.md",
        b"# Decision\n\n[Policy](bf://fixture/projects/policy.md?rel=depends-on#retention) supports this choice.\n",
    )
    ref = "bf://fixture/projects/policy.md#retention"
    before = retrieve.read([brain], ref)
    capture = run("capture", before)
    brain.write("projects/policy.md", b"# Policy\n\n## Retention {#retention}\n\nKeep no versions.\n")
    after = retrieve.read([brain], ref)
    assert run("compare", capture, after)["state"] == "changed"
    assert "Keep one version." in capture["text"]
    assert "Keep no versions." in str(after["text"])
    # Section reads intentionally omit graph context; inspect the parent only for impact review.
    assert "backlinks" not in after
    whole = retrieve.read([brain], "bf://fixture/projects/policy.md")
    groups = [group for group in cast("list[dict]", whole["backlinks"]) if group["relation"] == "depends-on"]
    assert len(groups) == 1
    assert "projects/decision.md" in json.dumps(groups)
    assert "origin" in json.dumps(groups)
    # The helper reads only stdin. A changed answer does not silently rewrite the conclusion.
    assert brain.read("projects/decision.md").startswith(b"# Decision")
