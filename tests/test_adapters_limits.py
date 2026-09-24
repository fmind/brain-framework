"""Provider limits apply while a producer runs, including after it closes stdout."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import pytest

from conftest import ROOT


@pytest.mark.parametrize("adapter", ["google-calendar.py", "google-drive-folders.py", "git-history.py"])
@pytest.mark.parametrize("mode", ["overflow", "timeout"])
def test_provider_output_is_bounded_and_child_is_reaped(adapter: str, mode: str, tmp_path: Path) -> None:
    run = runpy.run_path(str(ROOT / "examples/sensors" / adapter))["run"]
    pid = tmp_path / "pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        f"Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        + ("sys.stdout.buffer.write(b'x' * 8192); sys.stdout.flush()\n" if mode == "overflow" else "os.close(1)\n")
        + "time.sleep(60)\n"
    )
    expected = ValueError if mode == "overflow" else TimeoutError
    with pytest.raises(expected):
        run([sys.executable, str(producer)], limit=1024, timeout=1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)
