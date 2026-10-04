"""Check or regenerate docs/assets/watch.svg: the dashboard over fictional history for examples/watch.

`mise run check:screenshot` only checks; run `mise run generate:screenshot` after changing the dashboard or Rich.
"""

from __future__ import annotations

import argparse
import io
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rich.console import CONSOLE_SVG_FORMAT, Console

from bf.collect import next_due
from bf.config import load
from bf.history import log_path
from bf.storage import Store
from bf.watch import Dashboard, Row, Sort, State

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = "docs/assets/watch.svg"
WIDTH, HEIGHT = 120, 20
# A fixed clock keeps ages, due times and timestamps identical on every run.
NOW = datetime(2026, 9, 1, 9, 5, tzinfo=UTC)


def at(seconds: float) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat()


# Fictional history 42 seconds after the demo's first cycle: calendar has updated its record once, and the
# failed sensor waits one minute of failure backoff from its first run, so later cycles complete.
ROWS = [
    Row(
        "sensor",
        "calendar",
        State.FRESH,
        True,
        30,
        success=at(-12),
        next_due=at(18),
        records=1,
        added=0,
        updated=1,
        removed=0,
        elapsed_seconds=0.04,
        output_bytes=128,
    ),
    Row("sensor", "disabled", State.DISABLED, True, 60),
    Row(
        "sensor",
        "git",
        State.FRESH,
        True,
        60,
        success=at(-42),
        next_due=at(18),
        records=1,
        added=1,
        updated=0,
        removed=0,
        elapsed_seconds=0.03,
        output_bytes=122,
    ),
    Row("sensor", "manual", State.MANUAL, True, 0),
    Row(
        "sensor",
        "unavailable",
        State.FAILED,
        True,
        60,
        next_due=at(18),
        error="program exited with status 1; nothing was written",
        log=log_path("unavailable"),
    ),
]


def svg() -> str:
    names, config = {row.name for row in ROWS}, load(Store(ROOT / "examples/watch"))
    if names != set(config.sensors):
        raise SystemExit("render_watch.py: rows no longer match examples/watch/bf.yaml")
    # The failed row follows update's backoff rule from its failure in the first cycle, 42 seconds ago.
    failed = next(row for row in ROWS if row.status is State.FAILED)
    due = next_due(config.sensors[failed.name], {"error": failed.error, "run": at(-42)}, NOW)
    if due is None or due.isoformat() != failed.next_due:
        raise SystemExit("render_watch.py: the failed row's next due time no longer follows the failure backoff")
    # Contributors' terminals decide fonts; the published asset uses the project's fonts without web requests.
    template = re.sub(r"\n\s*@font-face \{\{.*?\}\}", "", CONSOLE_SVG_FORMAT, flags=re.DOTALL)
    template = template.replace("font-family: Fira Code, monospace;", 'font-family: "Google Sans Code", monospace;')
    template = template.replace("font-family: arial;", 'font-family: "Google Sans", sans-serif;')
    if "Fira Code" in template or "arial" in template:
        raise SystemExit("render_watch.py: Rich changed its SVG template; update the font replacements")
    dashboard = Dashboard(
        "watch-demo",
        rows=ROWS,
        sort=Sort.STATE,
        next_check=3,
        message="Update complete",
    )
    console = Console(
        file=io.StringIO(),
        record=True,
        width=WIDTH,
        height=HEIGHT,
        color_system="truecolor",
        force_terminal=True,
        no_color=False,
        legacy_windows=False,
    )
    with patch("bf.watch.time.monotonic", return_value=0.0):
        console.print(dashboard.render(WIDTH, HEIGHT, NOW))
    image = console.export_svg(title="Brain Framework · fictional offline demo", code_format=template)
    # The whitespace check rejects trailing spaces; text spaces are &#160; entities, so markup indentation is inert.
    return "\n".join(line.rstrip() for line in image.splitlines()) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Regenerate; the default checks without changing files.")
    args = parser.parse_args()
    expected, store = svg(), Store(ROOT)
    if args.write:
        store.write(OUTPUT, expected.encode())
    elif store.read(OUTPUT, 1 << 20).decode() != expected:
        parser.exit(1, f"{OUTPUT}: dashboard drift; run mise run generate:screenshot\n")


if __name__ == "__main__":
    main()
