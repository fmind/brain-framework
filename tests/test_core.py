"""Observable evidence, retrieval and failure boundaries."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fkf.config import load, yaml_object
from fkf.evaluate import evaluate
from fkf.index import CACHE, MANIFEST, build, cache_state, database, note
from fkf.models import Collection, Config, Error, Query, Record, Source, decode, encode, timestamp
from fkf.retrieve import bounded
from fkf.retrieve import context as service_context
from fkf.retrieve import find as service_find
from fkf.retrieve import read as service_read
from fkf.storage import Store, discover, relative, state_store, writer


def find(store: Store, query: Query) -> dict[str, Any]:
    return json.loads(encode(service_find(store, query)))


def context(store: Store, query: Query, budget: int = 850) -> dict[str, Any]:
    return json.loads(encode(service_context(store, query, budget)))


def read(store: Store, uri: str) -> dict[str, Any]:
    return json.loads(encode(service_read(store, uri)))


def test_decision_recovery_and_differential_fallback(base: Store) -> None:
    query = Query(text="offline retrieval")
    fallback = find(base, query)
    assert fallback["index"] == "missing"
    report = build(base)
    assert report["entries"] == 3
    indexed = find(base, query)
    assert indexed["index"] == "ready"
    assert indexed["items"] == fallback["items"]
    assert indexed["items"][0]["uri"] == "wiki/project.md"
    base.write(CACHE, b"broken")
    corrupt = find(base, query)
    assert corrupt["index"] == "corrupt"
    assert corrupt["items"] == fallback["items"]


def test_stale_changed_added_removed_notes(base: Store) -> None:
    build(base)
    base.write("wiki/new.md", b"# New decision\n\nUse simpler retrieval.\n")
    assert cache_state(base) == "stale"
    assert find(base, Query(text="simpler"))["items"][0]["uri"] == "wiki/new.md"
    build(base)
    base.write("wiki/new.md", b"# Superseded\n\nUse durable files.\n")
    assert cache_state(base) == "stale"
    build(base)
    (base.root / "wiki/new.md").unlink()
    assert cache_state(base) == "stale"


def test_touch_is_fresh_and_manifest_damage_falls_back(base: Store) -> None:
    build(base)
    path = base.root / "wiki/project.md"
    os.utime(path, ns=(1, 1))
    assert cache_state(base) == "ready"
    for value in [b"null", b"[]", b"{}", b'{"version":1,"files":null}', b"invalid"]:
        base.write(MANIFEST, value)
        assert cache_state(base) == "corrupt"
        assert find(base, Query(text="offline"))["items"]


def test_exact_identity_source_filter_time_and_empty_query(base: Store) -> None:
    build(base)
    assert find(base, Query(text="repo:example/project"))["items"][0]["kind"] == "note"
    records = find(base, Query(text="meeting:decision-1", source="meetings"))["items"]
    assert records[0]["kind"] == "record"
    assert not find(base, Query(text="offline", source="absent"))["items"]
    assert all(
        item["kind"] == "note" for item in find(base, Query(text="offline", after="2027-01-01T00:00:00Z"))["items"]
    )
    assert not find(base, Query(text='" OR * -- ; DROP TABLE entries'))["items"]


@pytest.mark.parametrize("budget", [128, 129, 256, 850, 4096])
def test_context_budget_includes_notice_receipt_and_multibyte_text(base: Store, budget: int) -> None:
    base.write("wiki/unicode.md", ("# Retrieval\n\n" + "retrieval été 日本語 " * 500).encode())
    pack = context(base, Query(text="retrieval"), budget)
    assert len(encode(pack)) <= budget * 4
    assert pack["notice"]
    assert pack["omitted"] >= 0


@pytest.mark.parametrize("budget", [0, 127, 16385])
def test_invalid_budget(base: Store, budget: int) -> None:
    with pytest.raises(Error, match="budget"):
        context(base, Query(text="retrieval"), budget)


def test_exact_reads_notes_fragments_captures_aliases_and_missing(base: Store) -> None:
    assert "Provider retention" in read(base, "wiki/project.md#reason")["text"]
    assert "Keep stored" in read(base, "repo:example/project")["text"]
    stored = read(base, "meeting:decision-1")
    assert stored["record"]["id"] == "decision-1"
    assert read(base, stored["uri"]) == stored
    assert len(read(base, "records/meetings/fixture.json")["collection"]["records"]) == 2
    for uri in ["fkf.yaml", "sources/secret.py", "https://example.invalid", "wiki/project.md#absent", "x" * 8193]:
        with pytest.raises(Error):
            read(base, uri)


def test_public_output_limit() -> None:
    with pytest.raises(Error, match="narrow"):
        bounded({"text": "x" * 100}, 50)


def test_duplicate_capture_rejected(base: Store) -> None:
    base.write("records/meetings/duplicate.json", base.read("records/meetings/fixture.json"))
    with pytest.raises(Error, match="duplicate captured"):
        build(base)


def test_invalid_document_preserves_previous_cache(base: Store) -> None:
    build(base)
    previous = base.read(CACHE)
    base.write("records/bad.json", b'{"version":999}')
    with pytest.raises(Error, match="invalid evidence"):
        build(base)
    assert base.read(CACHE) == previous


def test_concurrent_change_keeps_the_open_generation_and_is_named_afterwards(base: Store) -> None:
    build(base)
    with database(base) as (connection, state):
        base.write("wiki/concurrent.md", b"# Concurrent edit\n\nHelium evidence.\n")
        assert state == "ready"
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 3
    later = find(base, Query(text="helium"))
    assert later["index"] == "stale"
    assert later["items"][0]["uri"] == "wiki/concurrent.md"


def test_read_only_index_survives_a_rebuild_while_open(base: Store) -> None:
    build(base)
    with database(base) as (connection, _state):
        base.write("wiki/replaced.md", b"# Replaced generation\n")
        build(base)
        assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 3
    assert find(base, Query(text="replaced"))["index"] == "ready"


def test_absent_note_and_base_are_named(base: Store, tmp_path: Path) -> None:
    with pytest.raises(Error, match="not found"):
        read(base, "wiki/absent.md")
    with pytest.raises(Error, match="does not exist"):
        discover(str(tmp_path / "nowhere"))


@pytest.mark.parametrize(
    "text",
    [
        b"---\ntitle: bad",
        b"---\ntitle: 12\n---\nbody",
        b"---\naliases: 3\n---\nbody",
        b"---\nlinks: [3]\n---\nbody",
        b"\xff",
    ],
)
def test_bad_notes(text: bytes) -> None:
    with pytest.raises(Error):
        note("wiki/bad.md", text)


def test_markdown_links_are_explicit() -> None:
    value = note(
        "wiki/folder/note.md",
        b"# Decision\n\n[Evidence](../source.md#reason) [Web](https://example.com) [same](#here) [outside](../../../private)",
    )
    assert "wiki/source.md#reason" in value.links
    assert "https://example.com" in value.links
    assert "wiki/folder/note.md#here" in value.links
    assert note("wiki/untitled.md", b"plain text").title == "untitled"


def test_evaluation_checks_delivery_text_forbidden_and_empty(base: Store) -> None:
    base.write(
        "queries.yaml",
        b"""version: 1
cases:
  - name: decision
    query: offline retrieval
    expect: [wiki/project.md]
    excerpts: {wiki/project.md: [Keep stored reads offline]}
    reads: {meeting:decision-1: [team chose offline]}
  - name: absent
    query: zirconium
    empty: true
""",
    )
    assert evaluate(base)["passed"]
    base.write(
        "queries.yaml",
        b"""version: 1
cases:
  - name: missing
    query: lunch
    expect: [wiki/project.md]
    forbidden: [wiki/project.md]
    excerpts: {wiki/project.md: [missing text]}
""",
    )
    assert not evaluate(base)["passed"]
    for data in [b"cases: []", b"cases: [{name: bad, query: lunch}]"]:
        base.write("queries.yaml", data)
        with pytest.raises(Error):
            evaluate(base)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b"NaN", b"Infinity", b"{} {}", b"\xff", b"{"])
def test_json_rejects_ambiguous_or_nonfinite_input(raw: bytes) -> None:
    with pytest.raises(Error):
        decode(raw)


def test_json_preserves_unicode_large_integers_and_literal_line_separator() -> None:
    value = {"n": 2**100, "text": "a\u2028b 日本語"}
    assert decode(encode(value)) == value


@pytest.mark.parametrize(
    "raw",
    [
        b"a: 1\na: 2",
        b"a: &x [1]\nb: *x",
        b"!!python/object/apply:os.system [echo bad]",
        b"[]",
        b"[",
        b"1: value",
        (b"a: [" + b"[" * 40 + b"]" * 41),
    ],
)
def test_yaml_boundaries(raw: bytes) -> None:
    with pytest.raises(Error):
        yaml_object(raw)


def test_models_do_not_accept_silent_coercion_or_duplicate_ids() -> None:
    assert timestamp("2026-01-01T01:00:00+01:00") == "2026-01-01T00:00:00.000000Z"
    for value in ["bad", "2026-01-01"]:
        with pytest.raises(ValueError, match="timestamp"):
            timestamp(value)
    with pytest.raises(ValidationError):
        Record(id="x", title="\n")
    with pytest.raises(ValidationError):
        Config.model_validate({"name": "x", "sources": {"a": {"command": ["sh"], "timeout": "1"}}})
    with pytest.raises(ValidationError):
        Collection(source="x", captured="2026-01-01T00:00:00Z", records=[Record(id="x", title="X")] * 2)
    assert Record(id="x", title="X", links=["b", "a", "b"]).links == ["a", "b"]
    for command in [[""], ["{{base}}/x"], ["echo", "{{unknown}}"], ["echo", "\0"]]:
        with pytest.raises(ValidationError):
            Source(command=command)


def test_config_overlay_is_explicit(base: Store) -> None:
    base.write(
        "fkf.yaml",
        b"version: 1\nid: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n  sample: {command: [echo, '[]']}\n",
    )
    base.write("fkf.local.yaml", b"sources:\n  sample: {enabled: false}\n")
    assert not load(base).sources["sample"].enabled
    for raw in [b"name: forbidden", b"sources: []", b"sources: {sample: false}"]:
        base.write("fkf.local.yaml", raw)
        with pytest.raises(Error):
            load(base)


@pytest.mark.parametrize("path", ["../secret", "/outside/file", "wiki//x", "wiki/./x", "wiki/../x", "wiki\\x", ""])
def test_path_grammar(path: str) -> None:
    with pytest.raises(Error):
        relative(path)


def test_no_follow_read_write_and_traversal(base: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    (base.root / "wiki/link").symlink_to(outside, target_is_directory=True)
    for operation in [
        lambda: base.read("wiki/link/secret"),
        lambda: base.write("wiki/link/new", b"x"),
        lambda: base.files("wiki"),
    ]:
        with pytest.raises((Error, OSError)):
            operation()
    assert not (outside / "new").exists()
    (base.root / "wiki/direct").symlink_to(outside / "secret")
    with pytest.raises((Error, OSError)):
        base.read("wiki/direct")
    with pytest.raises(Error):
        base.write("wiki/direct", b"changed")
    assert (outside / "secret").read_text() == "private"


def test_bounded_files_immutable_writes_and_physical_lock(base: Store, tmp_path: Path) -> None:
    base.write("records/once.json", b"first", immutable=True)
    base.write("records/once.json", b"first", immutable=True)
    with pytest.raises(Error, match="immutable"):
        base.write("records/once.json", b"changed", immutable=True)
    with pytest.raises(Error, match="exceeds"):
        base.read("records/once.json", 2)
    alias = tmp_path / "alias"
    alias.symlink_to(base.root, target_is_directory=True)
    with writer(base), pytest.raises(Error, match="another writer"), writer(Store(alias)):
        pass
    with writer(base):
        pass


def test_discovery_and_state_boundary(base: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(base.root / "wiki")
    assert discover().root == base.root
    monkeypatch.setenv("FKF_BASE", str(base.root))
    assert discover().root == base.root
    assert discover(str(base.root)).root == base.root
    monkeypatch.setenv("XDG_STATE_HOME", str(base.root / "state"))
    with pytest.raises(Error, match="outside"):
        state_store(base.root)
    monkeypatch.delenv("FKF_BASE")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Error, match="no base"):
        discover()


def test_precise_unknown_identity_does_not_match_shared_words(base: Store) -> None:
    assert not find(base, Query(text="repo:example/absent"))["items"]
    assert "Provider retention" in read(base, "repo:example/project#reason")["text"]


def test_windows_recent_order_and_capture_deduplication(base: Store) -> None:
    records = [
        Record(id="old", title="Repeated retrieval", time="2026-08-01T00:00:00Z"),
        Record(id="new", title="Retrieval", time="2026-09-01T00:00:00Z"),
    ]
    for day in (1, 2):
        capture = Collection(source="timeline", captured=f"2026-09-0{day}T00:00:00Z", records=records)
        base.write(f"records/timeline/{day}.json", encode(capture.model_dump()))
    result = find(base, Query(text="retrieval", source="timeline", order="recent"))["items"]
    assert len(result) == 2
    assert result[0]["time"] == "2026-09-01T00:00:00.000000Z"
    assert all(item["captured"] == "2026-09-02T00:00:00.000000Z" for item in result)
    result = find(base, Query(text="retrieval", source="timeline", before="2026-09-01T00:00:00Z"))["items"]
    assert len(result) == 1
    assert result[0]["time"] == "2026-08-01T00:00:00.000000Z"
    with pytest.raises(ValidationError):
        Query(text="x", after="2026-09-02T00:00:00Z", before="2026-09-01T00:00:00Z")


def test_capture_references_bind_original_bytes(base: Store) -> None:
    original = read(base, "meeting:decision-1")
    path = original["path"]
    raw = base.read(path)
    # Re-serialization changes immutable bytes even when the decoded meaning agrees.
    base.write(path, raw + b" ")
    replacement = read(base, "meeting:decision-1")
    assert replacement["uri"] != original["uri"]
    with pytest.raises(Error, match="not found"):
        read(base, original["uri"])


def test_summary_and_authored_metadata_are_searchable(base: Store) -> None:
    base.write(
        "wiki/metadata.md",
        b"---\ntitle: Project\nsummary: Keep the xenon decision.\ntopic: zirconium\n---\n# Project\n",
    )
    assert find(base, Query(text="zirconium"))["items"][0]["uri"] == "wiki/metadata.md"
    assert "Keep the xenon decision" in context(base, Query(text="xenon"))["items"][0]["excerpt"]


def test_microsecond_window_boundary_and_deep_traversal(base: Store) -> None:
    capture = Collection(
        source="precise",
        captured="2026-09-01T00:00:00Z",
        records=[Record(id="fraction", title="Precise observation", time="2026-09-01T00:00:00.000001Z")],
    )
    base.write("records/precise/capture.json", encode(capture.model_dump()))
    assert find(base, Query(text="precise", after="2026-09-01T00:00:00Z"))["items"]
    assert not find(base, Query(text="precise", before="2026-09-01T00:00:00Z"))["items"]
    deep = base.root / "wiki"
    for _ in range(66):
        deep = deep / "nested"
    deep.mkdir(parents=True)
    with pytest.raises(Error, match="depth"):
        base.files("wiki")


def test_state_symlink_and_cache_symlink_never_write_outside(
    base: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside-state"
    outside.mkdir()
    alias = tmp_path / "state-alias"
    alias.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(alias))
    with pytest.raises(Error, match="symlinks"):
        state_store(base.root)
    assert not list(outside.iterdir())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "valid-state"))
    (base.root / ".fkf").symlink_to(outside, target_is_directory=True)
    with pytest.raises((Error, OSError)):
        build(base)
    assert not list(outside.iterdir())


def test_declared_links_are_canonical_while_markdown_links_are_relative() -> None:
    item = note("wiki/folder/project.md", b"---\nlinks: [wiki/source.md]\n---\n# Project\n\n[Other](../other.md)\n")
    assert item.links == ["wiki/other.md", "wiki/source.md"]


def test_context_delivers_late_passage_and_fits_small_budget(base: Store) -> None:
    base.write(
        "wiki/long.md",
        ("# Planning\n\n" + "Background material. " * 100 + "\nZirconium requires retained backups.").encode(),
    )
    for budget in (128, 256, 850):
        pack = context(base, Query(text="zirconium"), budget)
        assert pack["items"]
        assert "Zirconium" in pack["items"][0]["excerpt"]
        assert len(encode(pack)) <= budget * 4
    assert "Background material." in read(base, "wiki/long.md")["text"]


def test_literal_discovery_preserves_domain_words_and_single_letters(base: Store) -> None:
    base.write("wiki/languages.md", b"# Active changes\n\nC and R interoperability\n")
    for text in ("active", "changes", "C", "R"):
        assert find(base, Query(text=text))["items"][0]["uri"] == "wiki/languages.md"


def test_before_only_excludes_undated_records(base: Store) -> None:
    capture = Collection(
        source="undated", captured="2026-09-01T00:00:00Z", records=[Record(id="one", title="Unknown date")]
    )
    base.write("records/undated.json", encode(capture.model_dump()))
    assert not find(base, Query(text="unknown", before="2026-01-01T00:00:00Z"))["items"]


def test_same_size_edit_with_preserved_mtime_invalidates_cache(base: Store) -> None:
    base.write("wiki/edit.md", b"# Zirconium\n")
    build(base)
    path = base.root / "wiki/edit.md"
    info = path.stat()
    path.write_bytes(b"# Potassium\n")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    result = find(base, Query(text="potassium"))
    assert result["index"] == "stale"
    assert result["items"][0]["uri"] == "wiki/edit.md"
    assert not find(base, Query(text="zirconium"))["items"]


def test_sections_use_markdown_structure_and_preserve_original_text(base: Store) -> None:
    data = b"---\r\ntitle: Heading contract\r\n---\r\n# Actual\r\n\r\n```markdown\r\n# Example\r\n```\r\n\r\nStill actual.\r\n\r\n# Duplicate\r\nOne\r\n\r\n# Duplicate\r\nTwo\r\n\r\nSetext title\r\n============\r\nFinal\r\n"
    base.write("wiki/headings.md", data)
    assert "Still actual." in read(base, "wiki/headings.md#actual")["text"]
    assert read(base, "wiki/headings.md#duplicate-1")["text"] == "# Duplicate\r\nTwo\r\n\r\n"
    assert "Final" in read(base, "wiki/headings.md#setext-title")["text"]
    assert note("wiki/headings.md", data).title == "Heading contract"
    with pytest.raises(Error, match="heading"):
        read(base, "wiki/headings.md#example")


def test_unicode_separators_do_not_change_markdown_section_offsets(base: Store) -> None:
    base.write(
        "wiki/unicode.md", "# First\n\nLiteral separator a\u2028b.\n\n# Second\nExact second section.\n".encode()
    )
    assert read(base, "wiki/unicode.md#second")["text"] == "# Second\nExact second section.\n"


def test_short_evidence_preserves_answer_beyond_matched_words(base: Store) -> None:
    base.write(
        "wiki/short.md",
        (
            "# Contract\n\nRequired answer: preserve originals.\n\n"
            + "Background. " * 20
            + "Queryneedle locates this decision."
        ).encode(),
    )
    result = context(base, Query(text="queryneedle"))
    assert "preserve originals" in result["items"][0]["excerpt"]


def test_subject_words_survive_cleanup_and_diacritics_never_separate_terms(base: Store) -> None:
    base.write("wiki/resume.md", "# Résumé\n\nCareer summary for the réunion with the team.\n".encode())
    for text in ("update my resume with fkf", "resume", "RÉSUMÉ", "reunion", "réunion"):
        assert find(base, Query(text=text))["items"][0]["uri"] == "wiki/resume.md", text
    assert "réunion" in context(base, Query(text="reunion"), 128)["items"][0]["excerpt"]


def test_long_note_keeps_authored_lead_and_matching_passage(base: Store) -> None:
    base.write(
        "wiki/long-lead.md",
        (
            "# Decision\n\nKeep durable originals.\n\n"
            + "Background. " * 250
            + "\nQueryneedle explains the constraints."
        ).encode(),
    )
    result = context(base, Query(text="queryneedle"))
    assert "Keep durable originals" in result["items"][0]["excerpt"]
    assert "Queryneedle" in result["items"][0]["excerpt"]


def test_exact_reads_use_indexed_lookups_instead_of_scanning_the_corpus(
    base: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deciding snapshot recency per row made every exact read scan the whole corpus."""
    import fkf.retrieve as retrieval

    build(base)
    statements: list[tuple[str, tuple[object, ...]]] = []
    original = retrieval.database

    class Recorder:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            statements.append((sql, parameters))
            return self.connection.execute(sql, parameters)

    @contextmanager
    def recorded(store: Store) -> Any:
        with original(store) as (connection, state):
            yield Recorder(connection), state

    monkeypatch.setattr(retrieval, "database", recorded)
    with pytest.raises(Error, match="not found"):
        read(base, "record:absent")
    assert read(base, "meeting:decision-1")["record"]["id"] == "decision-1"
    assert statements
    with original(base) as (connection, _state):
        for sql, parameters in statements:
            plan = " ".join(str(row[-1]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))
            assert "SCAN" not in plan, f"{sql.strip()} -> {plan}"


def test_records_carry_a_stable_alias_to_cite_and_notes_do_not(base: Store) -> None:
    build(base)
    found = {item["kind"]: item for item in find(base, Query(text="offline retrieval"))["items"]}
    assert found["record"]["alias"] == "meeting:decision-1"
    assert found["record"]["uri"].startswith("record:")
    assert "alias" not in found["note"], "a note is cited by its own path"
    packed = context(base, Query(text="preserve durable evidence"), 850)
    record = next(item for item in packed["items"] if item["kind"] == "record")
    assert record["alias"] == "meeting:decision-1"
    assert len(encode(packed)) <= 850 * 4
    # The alias is citable: it resolves to the same evidence as the snapshot URI it accompanies.
    assert read(base, record["alias"])["record"]["id"] == read(base, str(record["uri"]))["record"]["id"]


def test_a_record_without_an_alias_has_a_source_identity(base: Store) -> None:
    capture = Collection(
        source="plain", captured="2026-09-01T00:00:00Z", records=[Record(id="p1", title="Unaliased observation")]
    )
    base.write("records/plain.json", encode(capture.model_dump()))
    build(base)
    item = find(base, Query(text="unaliased"))["items"][0]
    assert item["kind"] == "record"
    assert item["alias"] == "source:plain:p1"
    assert read(base, item["alias"])["record"]["id"] == "p1"
