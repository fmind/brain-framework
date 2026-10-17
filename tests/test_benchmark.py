"""The scale benchmark still measures correct results against the current internal APIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import ROOT

CORPUS = ("--records", "30", "--notes", "2", "--repeats", "1", "--batch", "5")


def benchmark(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """The report of a benchmark run on a tiny corpus."""
    result = subprocess.run(  # noqa: S603 - the repository's own benchmark script
        [sys.executable, str(ROOT / "scripts/benchmark_scale.py"), *CORPUS, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=cwd,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_benchmark_reports_every_measurement_on_a_tiny_corpus(tmp_path: Path) -> None:
    report = benchmark("--dir", str(tmp_path / "benchmark"))
    assert report["corpus"] == {"records": 30, "body_chars": 1024, "notes": 2, "repeats": 1, "batch": 5}
    reads = {"note_edit", "selective", "common", "question", "typo", "timeline", "home", "exact_read"}
    writes = {"changed_record", "unchanged_record", "upsert_add", "upsert_update", "upsert_remove"}
    assert set(report["seconds"]) == {"build", *reads, *writes}
    # The temporary brain is removed; only its parent directory remains.
    assert list((tmp_path / "benchmark").iterdir()) == []


def test_benchmark_rejects_more_records_than_one_collection_writes(tmp_path: Path) -> None:
    parent = tmp_path / "benchmark"
    result = subprocess.run(  # noqa: S603 - the repository's own benchmark script
        [sys.executable, str(ROOT / "scripts/benchmark_scale.py"), "--records", "100000", "--dir", str(parent)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # It used to build the whole corpus, then fail its single collection.
    assert result.returncode == 2
    assert "records 1-99999" in result.stderr
    assert not parent.exists()


def test_benchmark_ignores_a_relative_cache_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    benchmark(cwd=tmp_path, env={**os.environ, "HOME": str(home), "XDG_CACHE_HOME": "relative"})
    # As bf does, it falls back to ~/.cache instead of writing its corpus below the working directory.
    assert not (tmp_path / "relative").exists()
    assert list((home / ".cache" / "bf-benchmark").iterdir()) == []
