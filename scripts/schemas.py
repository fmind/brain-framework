"""Check or regenerate every published configuration and reply schema; compare JSON independent of formatting."""

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
    schemas = {kind: document(kind) for kind in get_args(Kind)}
    documents = {
        ROOT / "docs" / f"{'bf' if kind == 'brain' else kind}.schema.json": schema for kind, schema in schemas.items()
    }
    store = Store(ROOT)
    obsolete_roots = (
        (ROOT / "docs").glob("*.schema.json"),
        *((ROOT / "examples" / ex / "schemas").glob("*.json") for ex in ("brain", "context-hub")),
    )
    for path in sorted(p for group in obsolete_roots for p in group):
        if path in documents or path == ROOT / "examples/context-hub/schemas/jira.json":
            continue
        if args.write:
            path.unlink()
        else:
            parser.exit(1, f"{path.relative_to(ROOT).as_posix()}: obsolete schema; run mise run generate:schema\n")
    for path, schema in documents.items():
        rel = path.relative_to(ROOT).as_posix()
        if args.write:
            store.write(rel, encode(schema))
        else:
            try:
                actual = decode(store.read(rel, 1 << 20))
            except OSError, Error:
                parser.exit(1, f"{rel}: missing or invalid schema; run mise run generate:schema\n")
            if actual != schema:
                parser.exit(1, f"{rel}: schema drift; run mise run generate:schema\n")


if __name__ == "__main__":
    main()
