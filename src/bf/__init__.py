"""Brain Framework: plain-file brains for people and their agents."""

__version__ = "9.0.1"


def main() -> None:
    """Run the console interface."""
    from bf.cli import main as run

    run()
