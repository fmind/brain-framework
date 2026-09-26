"""Measure cache builds, incremental refreshes and searches over notes and records; never contact providers."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

from bf import records
from bf.index import refresh
from bf.models import Query, Record
from bf.retrieve import read, search
from bf.storage import Store, writer


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
        (root / "brain").mkdir()
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        store = Store(root / "brain")
        store.write("bf.yaml", b"version: 5\nname: benchmark\n")
        background = ("Routine project background. " * args.body_chars)[: args.body_chars]
        items = [
            Record(
                id=str(n),
                title=f"Decision {n}",
                text=background + "\nRetain evidence. " + ("Zirconium decision." if n == 0 else ""),
                time=f"2026-{n % 12 + 1:02d}-01T00:00:00Z",
                aliases=[f"decision:{n}"],
            )
            for n in range(args.records)
        ]
        records.upsert(store, "benchmark", items, snapshot=False)
        for n in range(args.notes):
            store.write(f"concepts/{n}.md", f"# Project {n}\n\n{background}\n\nKeep durable evidence.\n".encode())
        measurements = {}
        revision = 0
        note_revision = 0

        def change_note() -> dict[str, object]:
            nonlocal note_revision
            note_revision += 1
            store.write("concepts/0.md", f"# Edited revision{note_revision}\n\nZirconium note.\n".encode())
            return search([store], Query(text=f"revision{note_revision}"))

        def check_result(name: str, result: dict[str, object]) -> None:
            if result.get("problems") or result.get("stale"):
                raise RuntimeError(f"{name}: incomplete benchmark result")
            found = cast("list[dict[str, object]]", result.get("items", []))
            refs = {item["ref"] for item in found}
            valid = True
            if name == "note_edit":
                valid = refs == {"concepts/0.md"} and found[0]["title"] == f"Edited revision{note_revision}"
            elif name == "selective":
                valid = refs == ({"benchmark:0", "concepts/0.md"} if args.notes else {"benchmark:0"})
            elif name == "common":
                valid = len(found) == min(10, args.records + max(0, args.notes - 1))
            elif name == "timeline":
                since, until = str(result.get("since", "")), str(result.get("until", ""))
                expected = sum(since <= item.time < until for item in items)
                sources = cast("list[dict[str, object]]", result.get("sources", []))
                valid = (
                    result.get("page") == "2026-06"
                    and sum(cast("int", source["records"]) for source in sources) == expected
                )
            elif name == "home":
                valid = result.get("page") == "" and "memories" in cast("list[str]", result.get("pages", []))
            elif name == "exact_read":
                valid = result.get("ref") == "benchmark:0" and result.get("record") == items[0].model_dump(
                    exclude_defaults=True
                )
            elif name == "changed_record":
                valid = result.get("changed") == 1
            elif name == "unchanged_record":
                valid = result == {"added": 0, "updated": 0, "unchanged": 1, "removed": 0}
            if not valid:
                raise RuntimeError(f"{name}: unexpected benchmark result")

        def change_record() -> dict[str, object]:
            nonlocal revision
            revision += 1
            with writer(store):
                records.upsert(
                    store,
                    "benchmark",
                    [items[0].model_copy(update={"title": f"Edited decision {revision}"})],
                    snapshot=False,
                )
            return refresh(store)

        def unchanged_record() -> dict[str, object]:
            found = records.find(store, "benchmark", "0")
            if found is None:
                raise RuntimeError("benchmark fixture record is missing")
            with writer(store):
                return {**records.upsert(store, "benchmark", [found[1]], snapshot=False)}

        for name, operation in [
            ("build", lambda: refresh(store, full=True)),
            ("note_edit", change_note),
            ("selective", lambda: search([store], Query(text="zirconium"))),
            ("common", lambda: search([store], Query(text="evidence"))),
            ("timeline", lambda: read([store], "2026-06")),
            ("home", lambda: read([store])),
            ("exact_read", lambda: read([store], "decision:0")),
            ("changed_record", change_record),
            ("unchanged_record", unchanged_record),
        ]:
            if name == "note_edit" and not args.notes:
                continue
            timings = []
            for _ in range(args.repeats):
                start = time.perf_counter()
                result = operation()
                timings.append(time.perf_counter() - start)
                # Check every sample outside its timer; never publish timings for incorrect results.
                check_result(name, result)
            measurements[name] = {
                "min": round(min(timings), 3),
                "median": round(statistics.median(timings), 3),
                "max": round(max(timings), 3),
            }
        sys.stdout.write(json.dumps({"corpus": vars(args), "seconds": measurements}) + "\n")


if __name__ == "__main__":
    main()
