"""🧠 Brain Framework: from information to informed actions."""

import os
import sys

__version__ = "22.0.0"


def main() -> None:
    """Run the console interface."""
    # Locks and process groups use POSIX primitives; fail clearly before importing them elsewhere.
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
