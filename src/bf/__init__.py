"""Brain Framework: OKF Markdown notes, pure JSON memories, JSON Schema contracts, and offline retrieval."""

import os
import sys

__version__ = "29.0.1"


def main() -> None:
    """Run the console interface."""
    if os.name != "posix":
        raise SystemExit("bf: Brain Framework requires Linux or macOS")
    if sys.argv[1:] == ["--version"] and sys.stdout is not None:
        try:
            sys.stdout.buffer.write(f"{__version__}\n".encode())
            sys.stdout.flush()
        except OSError:
            pass
        else:
            return
    from bf.cli import main as run

    run()
