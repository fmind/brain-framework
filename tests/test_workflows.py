"""Decision workflows retain selected evidence without loading it into model context or mutating brains."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from bf import retrieve
from bf.models import Query
from bf.storage import Store
from conftest import Provider

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/bf-learn/scripts/evidence.py"
NOTE = {"brain": "example", "ref": "projects/policy.md#retention", "text": "## Retention\n\nKeep the latest version.\n"}
RECORD: dict = {
    "brain": "example",
    "ref": "mail:policy",
    "record": {"id": "policy", "title": "Policy", "text": "Keep one revision.", "attributes": {"observed": "old"}},
    "collection": {"state": "active", "freshness": "fresh"},
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


def test_capture_keeps_only_selected_evidence_and_comparison_is_compact() -> None:
    capture = run("capture", {**NOTE, "backlinks": [{"text": "UNRELATED"}], "claims": [{"text": "UNRELATED"}]})
    assert capture["text"] == NOTE["text"]
    assert "UNRELATED" not in json.dumps(capture)
    assert "external" not in capture
    assert len(capture["sha256"]) == 64
    # Captures use the canonical UTC instants of bf replies.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", capture["captured_at"])
    assert run("compare", capture, NOTE)["state"] == "unchanged"
    changed = run("compare", capture, {**NOTE, "text": "## Retention\n\nKeep selected older versions.\n"})
    assert changed["state"] == "changed"
    assert changed["content_changed"]
    assert "Keep" not in json.dumps(changed)
    assert len(json.dumps(changed)) < 512


def test_record_observation_does_not_retrigger_but_revision_and_fields_do() -> None:
    capture = run("capture", RECORD)
    observed = {**RECORD, "record": {**RECORD["record"], "attributes": {"observed": "new"}}}
    assert run("compare", capture, observed)["state"] == "unchanged"
    assert "external" not in run("compare", capture, observed)
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
        {**NOTE, "offset": 100},
        {**NOTE, "next_offset": 100},
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
        (capture, {**NOTE, "next_offset": 100}),
        (capture, {**NOTE, "offset": 100}),
        ({**capture, "next_offset": 100}, NOTE),
        ({**capture, "capture_version": True}, NOTE),
        ({**capture, "captured_at": "2026-09-25"}, NOTE),
    ]:
        result = execute("compare", before, after)
        assert result.returncode == 1
        assert not result.stdout


def test_real_reads_keep_history_after_replacement_and_explain_direct_impact(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nschema:\n  depends-on:\n    description: Needs review when evidence changes.\n"
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
    # The role page lists every dependent; an identity search explains each claim with its origin.
    dependents = retrieve.read([brain], "bf://fixture/projects/policy.md", rel="depends-on")["items"]
    assert [item["ref"] for item in cast("list[dict]", dependents)] == ["projects/decision.md"]
    found = retrieve.search([brain], Query(text="bf://fixture/projects/policy.md"))["items"]
    claims = next(item for item in cast("list[dict]", found) if item["ref"] == "projects/decision.md")["relations"]
    assert claims[0]["origin"] == "bf://fixture/projects/decision.md#decision"
    # Capture and compare read only stdin. A changed answer does not silently rewrite the conclusion.
    assert brain.read("projects/decision.md").startswith(b"# Decision")


LARGE = "## Retention {#retention}\n\n" + "Keep one revision per decision. " * 3000 + "\n"


def paged(brain: Store, ref: str) -> list[dict[str, object]]:
    """The genuine text pages of one exact read, in order."""
    replies = [retrieve.read([brain], ref)]
    while "next_offset" in replies[-1]:
        replies.append(retrieve.read([brain], ref, offset=cast("int", replies[-1]["next_offset"])))
    return replies


def test_read_assembles_paged_exact_replies_for_capture(brain: Store) -> None:
    # Exact replies above 32 KiB arrive in text pages; one page is never complete evidence.
    brain.write("projects/large.md", ("# Large\n\n" + LARGE).encode())
    ref = "projects/large.md#retention"
    assert len(paged(brain, ref)) > 2
    assert execute("capture", retrieve.read([brain], ref)).returncode == 1
    reply = subprocess.run(  # noqa: S603 - the bundled helper runs this checkout's bf on a synthetic brain
        [sys.executable, str(HELPER), "read", ref, "--brain", str(brain.root)],
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert reply.returncode == 0, reply.stderr
    whole = json.loads(reply.stdout)
    assert whole["ref"] == ref
    capture = run("capture", whole)
    assert capture["text"] == LARGE
    assert run("compare", capture, whole)["state"] == "unchanged"


@pytest.mark.parametrize("tamper", ["changed", "corrupt", "offset", "truncated", "missing", "ended"])
def test_read_rejects_changed_or_incomplete_pages(brain: Store, provider: Provider, tamper: str) -> None:
    brain.write("projects/large.md", ("# Large\n\n" + LARGE).encode())
    # A whole note's pages rebuild its file, which the digest verifies; a section's pages only share it.
    ref = "projects/large.md"
    replies = paged(brain, ref)
    first, second = replies[:2]
    replaced = {
        "changed": {**second, "sha256": "0" * 64},
        "corrupt": {**second, "text": str(second["text"]).replace("Keep", "Drop", 1)},
        "offset": {**second, "offset": 1},
        "truncated": {**second, "text": str(second["text"])[:-1]},
        "missing": None,
        "ended": NOTE,
    }[tamper]
    sequence = [first, *([replaced] if replaced else []), *replies[2:]]
    provider.install("bf", [{"match": ["read"], "stdout": reply} for reply in sequence])
    result = provider.run(
        "evidence.py", "read", ref, "--brain", "/brains/selected", folder="../skills/bf-learn/scripts"
    )
    assert result.returncode == 1
    assert not result.stdout
    assert "Keep" not in result.stderr
    assert provider.calls("bf")[1] == [
        "read",
        ref,
        "--brain",
        "/brains/selected",
        "--offset",
        str(first["next_offset"]),
    ]


def test_read_passes_whole_replies_and_rejects_option_like_refs(provider: Provider) -> None:
    provider.install("bf", [{"match": ["read"], "stdout": NOTE}])
    result = provider.run("evidence.py", "read", NOTE["ref"], "--brain", "brain", folder="../skills/bf-learn/scripts")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == NOTE
    for arguments in (("read", "--offset=1", "--brain", "brain"), ("read", NOTE["ref"]), ("read", "", "--brain", "x")):
        result = provider.run("evidence.py", *arguments, folder="../skills/bf-learn/scripts")
        assert result.returncode == 2
    assert provider.calls("bf") == [["read", NOTE["ref"], "--brain", "brain"]]
