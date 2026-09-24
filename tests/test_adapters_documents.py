"""Scoped local document snapshots expose useful text without following redirected files."""

from __future__ import annotations

import importlib.util
import os
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

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
    records = provider.records("local-documents.py", "work", str(root))
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


def test_document_root_cannot_be_redirected_through_an_ancestor(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "selected" / "documents"
    root.mkdir(parents=True)
    (root / "evidence.txt").write_text("evidence")
    (tmp_path / "redirect").symlink_to(root.parent, target_is_directory=True)
    result = provider.run("local-documents.py", "work", str(tmp_path / "redirect/documents"))
    assert result.returncode == 1
    assert not result.stdout


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
    executable.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        + ("os.write(1,b'x'*1024)\n" if flood else "")
        + "time.sleep(60)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.defpath)
    monkeypatch.setattr(documents, "FILE_BYTES", 256)
    monkeypatch.setattr(documents, "PDF_TIMEOUT", 1)
    with pytest.raises(ValueError, match="limit" if flood else "timed out"):
        documents.extract(b"%PDF synthetic", ".pdf")
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_office_structure_and_projected_output_are_bounded(
    documents: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "deep.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<p>" * 100 + "<t>deep</t>" + "</p>" * 100)
    with pytest.raises(ValueError, match="structure limit"):
        documents.extract(path.read_bytes(), ".docx")
    root = tmp_path / "documents"
    root.mkdir()
    (root / "small.txt").write_text("tiny")
    monkeypatch.setattr(documents, "TOTAL_BYTES", 64)
    with pytest.raises(ValueError, match="normalized document snapshot"):
        documents.collect("work", root, [])
