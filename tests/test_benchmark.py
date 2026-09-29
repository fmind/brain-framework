"""The scale benchmark still measures correct results against the current internal APIs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT


def test_benchmark_reports_every_measurement_on_a_tiny_corpus(tmp_path: Path) -> None:
    corpus = ["--records", "30", "--notes", "2", "--repeats", "1", "--batch", "5"]
    result = subprocess.run(  # noqa: S603 - the repository's own benchmark script
        [sys.executable, str(ROOT / "scripts/benchmark_scale.py"), *corpus, "--dir", str(tmp_path / "benchmark")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["corpus"] == {"records": 30, "body_chars": 1024, "notes": 2, "repeats": 1, "batch": 5}
    writes = {"upsert_add", "upsert_update", "upsert_remove"}
    assert {"build", "note_edit", "question", "exact_read", *writes} <= set(report["seconds"])
    # The temporary brain is removed; only its parent directory remains.
    assert list((tmp_path / "benchmark").iterdir()) == []
