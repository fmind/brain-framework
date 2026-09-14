"""Measure repeated retrieval over mixed notes and captures; never contact providers."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from fkf.index import build
from fkf.models import Collection, Query, Record, encode
from fkf.retrieve import context, read
from fkf.storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20_000)
    parser.add_argument("--body-chars", type=int, default=1024)
    parser.add_argument("--notes", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not (
        1 <= args.records <= 100_000
        and 64 <= args.body_chars <= 4096
        and 0 <= args.notes <= 1000
        and 1 <= args.repeats <= 20
    ):
        parser.error("limits: records 1-100000, body-chars 64-4096, notes 0-1000, repeats 1-20")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "base").mkdir()
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        store = Store(root / "base")
        store.write("fkf.yaml", b"version: 1\nid: aabbccddeeff00112233445566778899\nname: benchmark\n")
        background = ("Routine project background. " * args.body_chars)[: args.body_chars]
        for batch in range(0, args.records, 1000):
            capture = Collection(
                source="benchmark",
                captured="2026-09-01T00:00:00Z",
                records=[
                    Record(
                        id=str(n),
                        title=f"Decision {n}",
                        text=background + "\nRetain evidence. " + ("Zirconium decision." if n == 0 else ""),
                        aliases=[f"decision:{n}"],
                    )
                    for n in range(batch, min(batch + 1000, args.records))
                ],
            )
            store.write(f"records/{batch}.json", encode(capture.model_dump()))
        for n in range(args.notes):
            store.write(f"wiki/{n}.md", f"# Project {n}\n\n{background}\n\nKeep durable evidence.\n".encode())
        measurements = {}
        for name, operation in [
            ("fallback", lambda: context(store, Query(text="zirconium"))),
            ("build", lambda: build(store)),
            ("cached_selective", lambda: context(store, Query(text="zirconium"))),
            ("cached_common", lambda: context(store, Query(text="evidence"))),
            ("exact_read", lambda: read(store, "decision:0")),
        ]:
            timings = []
            for _ in range(args.repeats):
                start = time.perf_counter()
                operation()
                timings.append(time.perf_counter() - start)
            measurements[name] = {
                "min": round(min(timings), 3),
                "median": round(statistics.median(timings), 3),
                "max": round(max(timings), 3),
            }
        sys.stdout.write(json.dumps({"corpus": vars(args), "seconds": measurements}) + "\n")


if __name__ == "__main__":
    main()
