"""Selected passages retain precise provenance without turning annotations into source evidence."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest

from bf.collect import collect
from bf.evaluate import evaluate
from bf.models import Error, Query
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate
from conftest import ROOT, Provider

SOURCE = "https://example.com/website-brief"
REF = "highlights:reading/brief-clarity"
START = "2026-09-27T00:00:00Z"
END = "2026-09-28T00:00:00Z"


@pytest.fixture
def selected() -> dict[str, object]:
    export = json.loads((ROOT / "examples/sensors/highlights.json").read_text())
    return export["highlights"][0]


def save(path: Path, items: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"version": 1, "highlights": items}))


def test_highlights_preserve_passage_annotation_locator_and_dates(provider: Provider) -> None:
    records = provider.records("highlights.py", "reading", str(ROOT / "examples/sensors/highlights.json"))
    assert len(records) == 1
    record = records[0]
    assert record.id == "reading/brief-clarity"
    assert record.url == SOURCE
    assert record.text == "Visitors need a clear product explanation before signing up."
    assert record.attributes["annotation"] == "Review whether our first page explains who the product helps."
    assert record.attributes["locator"] == {"page": 2, "section": "Audience"}
    assert record.attributes["source_date"] == "2026-09-25"
    assert record.time.startswith("2026-09-27T12:00:00")
    assert "Review whether" not in record.text
    assert not record.aliases  # Sharing a source document does not merge distinct highlights.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../../private-marker"),
        ("title", "private-marker\nsecond line"),
        ("selection", ""),
        ("selection", "é" * 32769),
        ("selection", "private-marker\u0000"),
        ("annotation", {"private-marker": "unexpected object"}),
        ("source_url", "http://example.com/private-marker"),
        ("source_url", "https://name:private-marker@example.com/"),
        ("source_url", "https:///private-marker"),
        ("source_url", "https://example.com:invalid/private-marker"),
        ("locator", {}),
        ("locator", {"page": True}),
        ("locator", {"page": 0}),
        ("locator", {"section": ""}),
        ("locator", {"page": 2, "private-marker": "unknown field"}),
        ("captured_at", "2026-09-27T12:00:00"),
        ("captured_at", "2026-02-30T12:00:00Z"),
        ("source_date", "2026-02-30"),
        ("source_date", "20260925"),
        ("private-marker", "unknown field"),
    ],
)
def test_highlights_invalid_later_item_emits_no_partial_output(
    provider: Provider, tmp_path: Path, selected: dict[str, object], field: str, value: object
) -> None:
    path = tmp_path / "selected.json"
    save(path, [selected, {**selected, "id": "second", field: value}])
    result = provider.run("highlights.py", "reading", str(path))
    assert result.returncode == 1
    assert not result.stdout
    assert "highlight 2" in result.stderr
    assert "private-marker" not in result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":1,"version":1,"highlights":[]}',
        b'{"version":true,"highlights":[]}',
        b'{"version":2,"highlights":[]}',
        b'{"version":1,"highlights":null}',
        b'{"version":1,"highlights":NaN}',
        b'{"version":1,"highlights":[],"private-marker":1}',
        b'{"private-marker":',
        b"\xff\xfeprivate-marker",
    ],
)
def test_highlights_reject_ambiguous_or_malformed_exports(provider: Provider, tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "selected.json"
    path.write_bytes(payload)
    result = provider.run("highlights.py", "reading", str(path))
    assert result.returncode == 1
    assert not result.stdout
    assert "private-marker" not in result.stderr


def test_highlights_reject_duplicate_ids_and_excess_records(
    provider: Provider, tmp_path: Path, selected: dict[str, object]
) -> None:
    path = tmp_path / "selected.json"
    for items, diagnostic in [([selected, selected], "duplicate highlight id"), ([selected] * 1001, "1000 items")]:
        save(path, items)
        result = provider.run("highlights.py", "reading", str(path))
        assert result.returncode == 1
        assert not result.stdout
        assert diagnostic in result.stderr


@pytest.mark.parametrize("kind", ["file-link", "ancestor-link", "fifo", "oversized"])
def test_highlights_bound_input_and_refuse_redirected_or_special_files(
    provider: Provider, tmp_path: Path, selected: dict[str, object], kind: str
) -> None:
    folder = tmp_path / "exports"
    folder.mkdir()
    original = folder / "selected.json"
    save(original, [selected])
    path = tmp_path / "selected.json"
    if kind == "file-link":
        path.symlink_to(original)
    elif kind == "ancestor-link":
        (tmp_path / "redirect").symlink_to(folder, target_is_directory=True)
        path = tmp_path / "redirect/selected.json"
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as stream:
            stream.truncate((8 << 20) + 1)
    result = provider.run("highlights.py", "reading", str(path))
    assert result.returncode == 1
    assert not result.stdout


def test_highlights_are_deterministic_and_keep_exact_unicode_selection(
    provider: Provider, tmp_path: Path, selected: dict[str, object]
) -> None:
    path = tmp_path / "selected.json"
    first = {**selected, "selection": "  Décision éclairée.\n", "locator": {"section": "Résumé"}}
    second = {**selected, "id": "other", "locator": {"page": 3}}
    save(path, [second, first])
    output = provider.run("highlights.py", "reading", str(path))
    save(path, [first, second])
    repeated = provider.run("highlights.py", "reading", str(path))
    assert output.returncode == repeated.returncode == 0
    assert output.stdout == repeated.stdout
    assert json.loads(output.stdout)[0]["text"] == first["selection"]


def test_highlight_import_search_read_link_repeat_and_failure_preserve_evidence(
    brain: Store, selected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    brain.write("sensors/highlights.py", (ROOT / "examples/sensors/highlights.py").read_bytes())
    brain.write("inputs/highlights.json", b"{}")
    path = brain.root / "inputs/highlights.json"
    brain.write(
        "bf.yaml",
        json.dumps(
            {
                "version": 6,
                "name": "fixture",
                "sensors": {
                    "highlights": {
                        "command": [
                            "python3",
                            "sensors/highlights.py",
                            "reading",
                            "{{brain}}/inputs/highlights.json",
                        ],
                        "mode": "snapshot",
                        "fields": {"source-document": {"path": "/url"}},
                    }
                },
                "schema": {
                    "source-document": {
                        "description": "The document containing this selected passage.",
                        "type": "identity",
                        "cardinality": "one",
                        "relation": True,
                    }
                },
            }
        ).encode(),
    )
    save(path, [selected, {**selected, "id": "second"}])
    assert collect(brain, "highlights", start=START, end=END)["added"] == 2
    assert collect(brain, "highlights", start=START, end=END)["unchanged"] == 2
    found = search([brain], Query(text="product explanation", prefix="memories/highlights"))
    assert {item["ref"] for item in cast("list[dict]", found["items"])} == {REF, "highlights:reading/second"}
    reply = cast("dict", read([brain], REF)["record"])
    assert reply["fields"] == {"source-document": SOURCE}
    assert reply["attributes"]["locator"] == selected["locator"]
    assert not search([brain], Query(text="whether", prefix="memories/highlights"))["items"]
    brain.write(
        "projects/website.md",
        f"---\ntype: project\nstatus: draft\n---\n# New website\n\n## Decision\n"
        f"Explain the product first because visitors need clarity before signup. Evidence: [brief]({REF}).\n".encode(),
    )
    decision = search([brain], Query(text="clarity before signup", prefix="projects/website.md"))
    decision_ref = cast("list[dict]", decision["items"])[0]["ref"]
    assert "because visitors need clarity" in str(read([brain], decision_ref)["text"])
    brain.write(
        "evals/highlights.yaml",
        b"version: 5\ncases:\n  - name: recover-decision-reason\n    query: clarity before signup\n"
        b"    scope: projects/website.md\n    expect: [projects/website.md#decision]\n"
        b"  - name: locate-source-passage\n    read: highlights:reading/brief-clarity\n"
        b"    text: [https://example.com/website-brief, Audience, Visitors need a clear product explanation]\n",
    )
    assert validate(brain)["valid"]
    assert evaluate(brain)["passed"]
    save(path, [{**selected, "annotation": "Our interpretation changed; the passage did not."}])
    changed = collect(brain, "highlights", start=START, end=END)
    assert (changed["updated"], changed["removed"]) == (1, 1)
    assert cast("dict", read([brain], REF)["record"])["text"] == selected["selection"]
    stored = {name: brain.read(name) for name in brain.files("memories/highlights")}
    save(path, [selected, {**selected, "id": "second", "locator": {}}])
    with pytest.raises(Error):
        collect(brain, "highlights", start=START, end=END)
    assert {name: brain.read(name) for name in brain.files("memories/highlights")} == stored
    save(path, [])
    with pytest.raises(Error, match="snapshot returned no records"):
        collect(brain, "highlights", start=START, end=END)
    assert {name: brain.read(name) for name in brain.files("memories/highlights")} == stored
