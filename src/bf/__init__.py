"""🧠 Brain Framework: from information to informed actions."""

import os

__version__ = "18.1.0"


def main() -> None:
    """Run the console interface."""
    # Locks and process groups use POSIX primitives; fail clearly before importing them elsewhere.
    if os.name != "posix":
        raise SystemExit("bf: Brain Framework requires Linux or macOS")
    from bf.cli import main as run

    run()
