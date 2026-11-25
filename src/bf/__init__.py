"""Brain Framework: YAML-frontmatter Markdown notes, JSONL memories, JSON Schema contracts, and offline retrieval."""

import os

__version__ = "34.0.0"


def main() -> None:
    """Run the console interface."""
    if os.name != "posix":
        raise SystemExit("bf: Brain Framework requires Linux or macOS")
    from bf.cli import main as run

    run()
