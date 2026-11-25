"""Time JSONL record ingestion, incremental note compilation, validation, review, and FTS5 query over synthetic data.

The synthetic brain lives in a temporary folder under --dir, on disk by default: record writes fsync, and a RAM-backed
/tmp would hide that cost while holding the whole corpus in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from bf import engine, query, records, render, status
from bf.models import FORMAT, MAX_BATCH, MAX_FILES, NOTES_FILE
from bf.storage import Store


def _seed_batches(store: Store, source: str, items: list[dict[str, object]]) -> None:
    batch: list[dict[str, object]] = []
    batch_bytes = 2
    for item in items:
        item_len = len(json.dumps(item).encode()) + 2
        if batch and batch_bytes + item_len > MAX_BATCH - 65_536:
            records.ingest(store, source, json.dumps(batch).encode(), mode="upsert")
            batch, batch_bytes = [], 2
        batch.append(item)
        batch_bytes += item_len
    if batch:
        records.ingest(store, source, json.dumps(batch).encode(), mode="upsert")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=2_000)
    parser.add_argument("--body-chars", type=int, default=1024)
    parser.add_argument("--notes", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--batch", type=int, default=100, help="Records added, updated, and removed per write sample.")
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    cache = Path(xdg_cache) if Path(xdg_cache).is_absolute() else Path.home() / ".cache"
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
        items: list[dict[str, object]] = [
            {
                "id": str(n),
                "title": f"Decision {n}",
                "text": background
                + ("\nRetain evidence.\nRare tail marker." if n == args.records - 1 else "\nRetain evidence."),
                "created": f"2026-{n % 12 + 1:02d}-01T00:00:00Z",
                "updated": "2026-09-01T00:00:00Z",
            }
            for n in range(args.records)
        ]
        _seed_batches(store, "benchmark", items)
        for n in range(args.notes):
            store.write(
                f"concepts/{n}.md",
                (
                    f"---\ntype: concept\nstatus: stable\nupdated: 2026-09-10\ntags: [bench]\n---\n"
                    f"# Concept {n}\n\n## Details {{#details idx={n}}}\n\n{background}\n\n"
                    f"Evidence: [Decision](benchmark:{n % args.records}).\n"
                ).encode(),
            )

        churn = {"add": 0, "update": 0, "remove": 0}
        measurements: dict[str, dict[str, float]] = {}
        base_batch = items[: args.batch]
        base_payload = json.dumps(base_batch).encode()
        edited_payload = json.dumps(
            [{**rec, "text": background + "\nRevised decision."} for rec in base_batch]
        ).encode()
        active_source = ""

        def prep_write(kind: str) -> None:
            nonlocal active_source
            churn[kind] += 1
            active_source = f"churn-{kind}-{churn[kind]}"
            if kind != "add":
                records.ingest(store, active_source, base_payload)

        def run_write(kind: str) -> dict[str, object]:
            if kind == "add":
                return records.ingest(store, active_source, base_payload)
            if kind == "update":
                return records.ingest(store, active_source, edited_payload)
            return records.ingest(store, active_source, b"[]", mode="snapshot", allow_removal=True)

        corpus_reset = {"toggle": 0}
        corpus_payload = b""

        def prep_corpus_upsert() -> None:
            nonlocal corpus_payload
            corpus_reset["toggle"] += 1
            corpus_payload = json.dumps(
                [{**rec, "text": background + f"\nRevised pass {corpus_reset['toggle']}."} for rec in base_batch]
            ).encode()

        def prep_full_build() -> None:
            if store.exists(NOTES_FILE):
                store.delete(NOTES_FILE)

        def run_status_text() -> dict[str, Any]:
            render.format_status_text(status.status_summary(store, include_loops=True))
            return {"ok": True}

        operations: list[tuple[str, Any, Any]] = [
            ("build_full", prep_full_build, lambda: engine.build(store)),
            ("build_incremental", None, lambda: engine.build(store)),
            ("validate", None, lambda: engine.validate(store)),
            ("review", None, lambda: engine.review(store)),
            ("query_fts_scoped", None, lambda: query.query(store, "Retain evidence", scope="benchmark", limit=20)),
            ("query_fts_unscoped", None, lambda: query.query(store, "Rare tail marker", limit=20)),
            ("query_structured", None, lambda: query.query(store, None, kind="memory", scope="benchmark", limit=20)),
            ("read_indexed", None, lambda: query.read_target(store, f"benchmark:{args.records - 1}")),
            ("status_text", None, run_status_text),
            ("unchanged_record", None, lambda: records.ingest(store, "benchmark", json.dumps([items[0]]).encode())),
            (
                "upsert_update_corpus",
                prep_corpus_upsert,
                lambda: records.ingest(store, "benchmark", corpus_payload, mode="upsert"),
            ),
            ("upsert_add", lambda: prep_write("add"), lambda: run_write("add")),
            ("upsert_update", lambda: prep_write("update"), lambda: run_write("update")),
            ("snapshot_remove", lambda: prep_write("remove"), lambda: run_write("remove")),
        ]

        for name, prep, op in operations:
            if prep is not None:
                prep()
            op()  # untimed warmup
            timings: list[float] = []
            for _ in range(args.repeats):
                if prep is not None:
                    prep()
                start = time.perf_counter()
                result: dict[str, Any] = op()
                timings.append(time.perf_counter() - start)
                summary_obj = result.get("summary")
                err_count = summary_obj.get("errors", 0) if isinstance(summary_obj, dict) else 0
                has_err = (
                    not result.get("ok", True)
                    or bool(err_count)
                    or (summary_obj is None and bool(result.get("problems")))
                )
                if has_err:
                    raise RuntimeError(f"{name}: failed benchmark validation: {result}")
            measurements[name] = {
                "min": round(min(timings), 4),
                "median": round(statistics.median(timings), 4),
                "max": round(max(timings), 4),
            }

        notes_bytes = (store.root / NOTES_FILE).stat().st_size
        memories_bytes = (store.root / "memories/benchmark.jsonl").stat().st_size
        index_bytes = (store.root / ".bf/cache/index.sqlite3").stat().st_size
        max_rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        max_rss_kb = max_rss_raw // 1024 if sys.platform == "darwin" else max_rss_raw
        corpus = {key: value for key, value in vars(args).items() if key != "dir"}
        sys.stdout.write(
            json.dumps(
                {
                    "corpus": corpus,
                    "seconds": measurements,
                    "peak_rss_kib": max_rss_kb,
                    "notes_jsonl_bytes": notes_bytes,
                    "memories_jsonl_bytes": memories_bytes,
                    "index_sqlite_bytes": index_bytes,
                }
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
