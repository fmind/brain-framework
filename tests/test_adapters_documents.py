"""Scoped local document snapshots expose useful text without following redirected files."""

from __future__ import annotations

import importlib.util
import io
import os
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import TypeAdapter

from bf.models import Record
from conftest import ROOT, Provider


def test_local_documents_extract_text_office_and_saved_pages(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "decision.md").write_text("# Retention\n\nConserver les preuves.\n")
    (root / "page.html").write_text("<title>Reference</title><script>secret()</script><p>Useful evidence</p>")
    with zipfile.ZipFile(root / "meeting.docx", "w") as archive:
        archive.writestr("word/document.xml", "<document><p><t>Keep original evidence.</t></p></document>")
    with zipfile.ZipFile(root / "slides.pptx", "w") as archive:
        archive.writestr("ppt/slides/slide2.xml", "<slide><p><t>Second slide</t></p></slide>")
        archive.writestr("ppt/slides/slide1.xml", "<slide><p><t>First slide</t></p></slide>")
    with zipfile.ZipFile(root / "data.xlsx", "w") as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Budget</t></si></sst>")
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row></worksheet>',
        )
    (root / ".env").write_text("DO_NOT_COLLECT=secret")
    # Office's owner file for the open meeting.docx is not a ZIP archive: it would fail the whole snapshot.
    (root / "~$meeting.docx").write_bytes(b"\x05Owner" + bytes(150))
    result = provider.run("local-documents.py", "work", str(root))
    assert (result.returncode, result.stderr) == (0, "")
    records = TypeAdapter(list[Record]).validate_json(result.stdout)
    assert len(records) == 5
    by_id = {record.id: record for record in records}
    assert "Conserver les preuves" in by_id["work/decision.md"].text
    assert "Keep original evidence" in by_id["work/meeting.docx"].text
    assert "secret" not in by_id["work/page.html"].text
    assert by_id["work/slides.pptx"].text.index("First slide") < by_id["work/slides.pptx"].text.index("Second slide")
    assert "A1: Budget" in by_id["work/data.xlsx"].text
    assert "B1: 42" in by_id["work/data.xlsx"].text
    assert all(record.attributes.get("updated") and record.url.startswith("file:") for record in records)
    assert len({record.aliases[0] for record in records}) == 5


def test_local_documents_fail_closed_on_links_and_unsafe_xml(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "good.txt").write_text("Keep evidence")
    (root / "redirect.txt").symlink_to(root / "good.txt")
    failed = provider.run("local-documents.py", "work", str(root))
    assert failed.returncode == 1
    assert not failed.stdout
    (root / "redirect.txt").unlink()
    with zipfile.ZipFile(root / "bad.docx", "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE doc [<!ENTITY x "hidden">]><doc>&x;</doc>')
    failed = provider.run("local-documents.py", "work", str(root))
    assert failed.returncode == 1
    assert not failed.stdout


def test_local_documents_mark_truncation_and_use_bounded_pdf_converter(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "long.txt").write_text("évidence " * 12000)
    (root / "paper.pdf").write_bytes(b"%PDF-1.7\nsynthetic")
    provider.install("pdftotext", [{"match": ["-layout"], "stdout": "Page one\fPage two"}])
    records = provider.records("local-documents.py", "work", str(root))
    assert records[0].attributes["partial"] is True
    assert "Page two" in records[1].text
    assert len(provider.calls("pdftotext")) == 1


def test_local_documents_keep_text_before_the_office_element_bound(provider: Provider, tmp_path: Path) -> None:
    # One part with more than 100,000 elements keeps its first 100,000 and marks the record partial.
    root = tmp_path / "documents"
    root.mkdir()
    (root / "notes.txt").write_text("Keep evidence")
    body = "<p><t>Kept heading</t></p><p>" + "<r/>" * 100_000 + "</p><p><t>Beyond the bound</t></p>"
    with zipfile.ZipFile(root / "large.docx", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f"<document>{body}</document>")
    records = {record.id: record for record in provider.records("local-documents.py", "work", str(root))}
    large = records["work/large.docx"]
    assert "Kept heading" in large.text
    assert "Beyond the bound" not in large.text
    assert large.attributes["partial"] is True
    assert records["work/notes.txt"].attributes["partial"] is False


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_document_xml_rejects_wide_encoded_entities(provider: Provider, tmp_path: Path, encoding: str) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    xml = '<?xml version="1.0"?><!DOCTYPE doc [<!ENTITY x "hidden">]><document><p><t>&x;</t></p></document>'
    with zipfile.ZipFile(root / "bad.docx", "w") as archive:
        archive.writestr("word/document.xml", xml.encode(encoding))
    result = provider.run("local-documents.py", "work", str(root))
    assert result.returncode == 1
    assert not result.stdout
    assert "hidden" not in result.stderr


def test_local_documents_keep_legacy_text_and_skip_unnameable_entries(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    (root / "legacy.csv").write_bytes(b"caf\xe9;prix\n")
    (root / "notes.txt").write_text("Keep evidence")
    # Names BF cannot store in a record id: not UTF-8 (a lone surrogate), or with a control character.
    skipped = 1
    try:
        (root / os.fsdecode(b"caf\xe9")).mkdir()
    except OSError:
        pass  # APFS stores only UTF-8 names
    else:
        (root / os.fsdecode(b"caf\xe9/inside.txt")).write_text("hidden by its folder")
        skipped += 1
    (root / "tab\tname.txt").write_text("unnameable")
    result = provider.run("local-documents.py", "work", str(root))
    assert result.returncode == 0, result.stderr
    records = {record.id: record for record in TypeAdapter(list[Record]).validate_json(result.stdout)}
    assert set(records) == {"work/legacy.csv", "work/notes.txt"}
    assert records["work/legacy.csv"].text == "caf�;prix\n"
    assert records["work/legacy.csv"].attributes["partial"] is True
    assert records["work/notes.txt"].attributes["partial"] is False
    assert f"skipped {skipped} entr" in result.stderr


def test_local_documents_name_the_file_that_fails(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "documents"
    (root / "sub").mkdir(parents=True)
    (root / "sub/paper.pdf").write_bytes(b"%PDF-1.7\nprivate-marker")
    provider.install("pdftotext", [{"match": ["-layout"], "code": 1, "stderr": "private-marker"}])
    result = provider.run("local-documents.py", "work", str(root))
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == "Local document collection failed: sub/paper.pdf: PDF conversion failed.\n"
    (root / "sub/paper.pdf").unlink()
    (root / "sub/broken.docx").write_bytes(b"private-marker")
    result = provider.run("local-documents.py", "work", str(root))
    assert result.returncode == 1
    assert "sub/broken.docx: damaged or unsupported document" in result.stderr
    assert "private-marker" not in result.stderr


def _archive(member: str, compression: int = zipfile.ZIP_STORED) -> bytearray:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        archive.writestr(member, "<document><p><t>" + "private-marker " * 400 + "</t></p></document>")
    return bytearray(buffer.getvalue())


def _corrupt_stream() -> bytes:
    data = _archive("word/document.xml", zipfile.ZIP_DEFLATED)
    start = 30 + len("word/document.xml")  # the compressed stream follows the local file header
    data[start : start + 20] = bytes(byte ^ 0xFF for byte in data[start : start + 20])
    return bytes(data)


def _encrypted_member() -> bytes:
    data = _archive("word/document.xml")
    data[6] |= 1  # the encryption flag in the local file header
    data[data.index(b"PK\x01\x02") + 8] |= 1  # and in the central directory
    return bytes(data)


@pytest.mark.parametrize("damage", [_corrupt_stream, _encrypted_member], ids=["corrupt-stream", "encrypted"])
def test_damaged_office_documents_are_named(provider: Provider, tmp_path: Path, damage: Callable[[], bytes]) -> None:
    root = tmp_path / "documents"
    (root / "sub").mkdir(parents=True)
    (root / "sub/report.docx").write_bytes(damage())
    result = provider.run("local-documents.py", "work", str(root))
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "Local document collection failed: sub/report.docx: damaged or unsupported document; repair or exclude it.\n"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read every file")
@pytest.mark.parametrize("entry", ["sub/locked.txt", "private"])
def test_unreadable_entries_are_named_instead_of_dropped(provider: Provider, tmp_path: Path, entry: str) -> None:
    root = tmp_path / "documents"
    (root / "sub").mkdir(parents=True)
    (root / "sub/locked.txt").write_text("evidence")
    (root / "private").mkdir()
    locked = root / entry
    locked.chmod(0)
    try:
        result = provider.run("local-documents.py", "work", str(root))
    finally:
        locked.chmod(0o700)
    # Skipping would remove the entry's saved records from the snapshot, so collection fails instead.
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        f"Local document collection failed: {entry}: cannot be read; check its permissions or exclude it.\n"
    )


def test_missing_pdf_converter_is_named(documents: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ValueError, match="pdftotext is not installed"):
        documents.extract(b"%PDF synthetic", ".pdf")


def test_document_root_cannot_be_redirected_through_an_ancestor(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "selected" / "documents"
    root.mkdir(parents=True)
    (root / "evidence.txt").write_text("evidence")
    (tmp_path / "redirect").symlink_to(root.parent, target_is_directory=True)
    result = provider.run("local-documents.py", "work", str(tmp_path / "redirect/documents"))
    assert result.returncode == 1
    assert not result.stdout
    # The refusal names its cause and fix, such as a linked {{home}} or Documents folder, not permissions.
    assert result.stderr == (
        "Local document collection failed: ROOT or one of its parent folders is a symbolic link or not a "
        "directory; configure its physical path, as realpath prints it.\n"
    )


@pytest.fixture
def documents() -> ModuleType:
    path = ROOT / "examples/sensors/local-documents.py"
    if not path.exists():
        path = ROOT / "sensors/local-documents.py"
    spec = importlib.util.spec_from_file_location("local_documents", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flood", [False, True])
def test_pdf_converter_is_killed_on_timeout_or_excess_output(
    documents: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flood: bool
) -> None:
    executable, marker = tmp_path / "pdftotext", tmp_path / "converter.pid"
    # A shell fake starts in milliseconds; exec keeps the recorded PID for the sleeping converter.
    executable.write_text(
        f"#!/bin/sh\necho $$ > '{marker}'\n" + ("head -c 1024 /dev/zero\n" if flood else "") + "exec sleep 60\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.defpath)
    monkeypatch.setattr(documents, "FILE_BYTES", 256)
    # The output test must reach its byte limit before the timeout.
    monkeypatch.setattr(documents, "PDF_TIMEOUT", 10 if flood else 2)
    with pytest.raises(ValueError, match="limit" if flood else "timed out"):
        documents.extract(b"%PDF synthetic", ".pdf")
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_office_structure_and_projected_output_are_bounded(
    documents: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nesting beyond 64 levels stops parsing: the text before the cut stays, marked partial.
    path = tmp_path / "deep.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<p><t>shallow</t>" + "<p>" * 100 + "<t>deep</t>" + "</p>" * 101)
    text, partial = documents.extract(path.read_bytes(), ".docx")
    assert (text.split("\n")[0], "deep" in text, partial) == ("shallow", False, True)
    # A cut shared-string table leaves later cells empty instead of calling the workbook damaged.
    monkeypatch.setattr(documents, "MAX_ELEMENTS", 3)
    path = tmp_path / "wide.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Budget</t></si><si><t>Hidden</t></si></sst>")
        archive.writestr("xl/worksheets/sheet1.xml", '<c r="A1" t="s"><v>1</v></c>')
    assert documents.extract(path.read_bytes(), ".xlsx") == ("xl/worksheets/sheet1.xml\nA1: ", True)
    monkeypatch.undo()
    root = tmp_path / "documents"
    root.mkdir()
    (root / "small.txt").write_text("tiny")
    monkeypatch.setattr(documents, "TOTAL_BYTES", 64)
    with pytest.raises(ValueError, match="normalized document snapshot"):
        documents.collect("work", root, [])
