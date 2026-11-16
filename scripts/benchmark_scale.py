"""Time pure JSON record ingestion, note compilation, validation, and review over synthetic notes and records.

The synthetic brain lives in a temporary folder under --dir, on disk by default: record writes fsync, and a RAM-backed
/tmp would hide that cost while holding the whole corpus in memory.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from bf import engine, records
from bf.models import FORMAT, MAX_FILES
from bf.storage import Store, xdg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=2_000)
    parser.add_argument("--body-chars", type=int, default=1024)
    parser.add_argument("--notes", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--batch", type=int, default=100, help="Records added, updated, and removed per write sample.")
    cache = xdg("XDG_CACHE_HOME", ".cache")
    parser.add_argument("--dir", type=Path, default=cache / "bf-benchmark", help="Parent of the temporary brain.")
    args = parser.parse_args()
    if not (
        1 <= args.records < MAX_FILES
        and 64 <= args.body_chars <= 4096
        and 1 <= args.notes <= 1000
        and 1 <= args.repeats <= 20
        and 1 <= args.batch <= 10_000
    ):
        parser.error(
            f"limits: records 1-{MAX_FILES - 1}, body-chars 64-4096, notes 1-1000, repeats 1-20, batch 1-10000"
        )
    args.batch = min(args.batch, args.records)
    args.dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bf-benchmark-", dir=args.dir) as temporary:
        root = Path(temporary) / "brain"
        engine.init_brain(root, name="benchmark")
        store = Store(root)
        store.write("bf.yaml", f"version: {FORMAT}\nname: benchmark\n".encode())
        background = ("Routine project background. " * args.body_chars)[: args.body_chars]
        items = [
            {
                "id": str(n),
                "title": f"Decision {n}",
                "text": background + "\nRetain evidence.",
                "time": f"2026-{n % 12 + 1:02d}-01T00:00:00Z",
                "updated": "2026-09-01T00:00:00Z",
            }
            for n in range(args.records)
        ]
        records.ingest(store, "benchmark", json.dumps(items).encode(), mode="snapshot", allow_removal=True)
        for n in range(args.notes):
            rec_rel = records.record_file("benchmark", str(n % args.records))
            store.write(
                f"concepts/{n}.md",
                (
                    f"---\ntype: concept\nstatus: stable\nupdated: 2026-09-10\ntags: [bench]\n---\n"
                    f"# Concept {n}\n\n## Details {{#details idx={n}}}\n\n{background}\n\n"
                    f"Evidence: [Decision](../{rec_rel}).\n"
                ).encode(),
            )

        churn = {"add": 0, "update": 0, "remove": 0}
        measurements: dict[str, dict[str, float]] = {}

        def write_batch(kind: str) -> dict[str, object]:
            churn[kind] += 1
            source = f"churn-{churn[kind]}"
            base_batch = items[: args.batch]
            if kind == "add":
                return records.ingest(store, source, json.dumps(base_batch).encode())
            records.ingest(store, source, json.dumps(base_batch).encode())
            if kind == "update":
                edited = [{**rec, "text": background + "\nRevised decision."} for rec in base_batch]
                return records.ingest(store, source, json.dumps(edited).encode())
            return records.ingest(store, source, b"[]", mode="snapshot", allow_removal=True)

        operations = [
            ("build", lambda: engine.build(store)),
            ("validate", lambda: engine.validate(store)),
            ("review", lambda: engine.review(store)),
            ("unchanged_record", lambda: records.ingest(store, "benchmark", json.dumps([items[0]]).encode())),
            ("upsert_add", lambda: write_batch("add")),
            ("upsert_update", lambda: write_batch("update")),
            ("snapshot_remove", lambda: write_batch("remove")),
        ]

        for name, op in operations:
            timings: list[float] = []
            for _ in range(args.repeats):
                start = time.perf_counter()
                result = op()
                timings.append(time.perf_counter() - start)
                if result.get("problems") or (name == "validate" and not result.get("valid")):
                    raise RuntimeError(f"{name}: failed benchmark validation: {result}")
            measurements[name] = {
                "min": round(min(timings), 4),
                "median": round(statistics.median(timings), 4),
                "max": round(max(timings), 4),
            }

        notes_bytes = sum(p.stat().st_size for p in (store.root / "memories/concepts").glob("*.json"))
        memories_bytes = sum(p.stat().st_size for p in (store.root / "memories/benchmark").glob("*.json"))
        corpus = {key: value for key, value in vars(args).items() if key != "dir"}
        sys.stdout.write(
            json.dumps(
                {
                    "corpus": corpus,
                    "seconds": measurements,
                    "notes_json_bytes": notes_bytes,
                    "memories_json_bytes": memories_bytes,
                }
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
