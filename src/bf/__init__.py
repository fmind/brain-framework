"""Brain Framework: OKF Markdown notes, JSONL memories, JSON Schema contracts, and offline retrieval."""

import os
import sys

__version__ = "33.2.0"


def main() -> None:
    """Run the console interface."""
    if os.name != "posix":
        raise SystemExit("bf: Brain Framework requires Linux or macOS")
    if sys.argv[1:] == ["--version"] and sys.stdout is not None:
        try:
            sys.stdout.buffer.write(f"{__version__}\n".encode())
            sys.stdout.flush()
        except BrokenPipeError:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.close(devnull)
            except OSError:
                pass
            return
        except OSError:
            pass
        else:
            return
    from bf.cli import main as run

    run()
