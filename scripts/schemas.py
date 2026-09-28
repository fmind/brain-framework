"""Check or regenerate every published configuration schema; compare JSON independent of formatting."""

import argparse
from pathlib import Path
from typing import get_args

from bf.models import Error, decode, encode
from bf.schemas import Kind, document
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Regenerate; the default checks without changing files.")
    args = parser.parse_args()
    # Generate everything before replacing a file; an invalid model must not leave a partial update.
    documents = {
        ROOT / "docs" / f"{'bf' if kind == 'brain' else kind}.schema.json": document(kind) for kind in get_args(Kind)
    }
    store = Store(ROOT)
    for path, schema in documents.items():
        if args.write:
            store.write(path.relative_to(ROOT).as_posix(), encode(schema))
        else:
            try:
                actual = decode(store.read(path.relative_to(ROOT).as_posix(), 1 << 20))
            except OSError, Error:
                parser.exit(1, f"{path.name}: missing or invalid schema; run mise run generate:schema\n")
            if actual != schema:
                parser.exit(1, f"{path.name}: schema drift; run mise run generate:schema\n")


if __name__ == "__main__":
    main()
