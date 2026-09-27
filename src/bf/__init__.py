"""🧠 AI Brain Factory: from information to informed action. Not for 🐙 mindflayers or 🧟 zombies."""

import os

__version__ = "13.0.2"


def main() -> None:
    """Run the console interface."""
    # Locks and process groups use POSIX primitives; fail clearly before importing them elsewhere.
    if os.name != "posix":
        raise SystemExit("bf: Brain Framework requires Linux or macOS")
    from bf.cli import main as run

    run()
