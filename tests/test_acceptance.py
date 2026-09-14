"""The reusable synthetic acceptance corpus exercises delivered answers and fallback parity."""

from pathlib import Path

import pytest

from fkf.evaluate import evaluate
from fkf.index import CACHE, build
from fkf.models import Collection, Record, encode
from fkf.storage import Store


@pytest.mark.parametrize("state", ["missing", "ready", "corrupt"])
def test_acceptance_corpus(tmp_path: Path, state: str) -> None:
    root = tmp_path / "acceptance"
    root.mkdir()
    store = Store(root)
    store.write("fkf.yaml", b"id: aabbccddeeff00112233445566778899\nname: acceptance\n")
    fixtures = Path(__file__).parent / "fixtures/retrieval"
    for path in fixtures.rglob("*"):
        if path.is_file():
            store.write(path.relative_to(fixtures).as_posix(), path.read_bytes())
    store.write(
        "wiki/long.md",
        ("# Planning\n\n" + "Background details. " * 100 + "\nZirconium requires durable originals.").encode(),
    )
    for day, decision in [(1, "Delete old originals."), (2, "Keep durable originals.")]:
        capture = Collection(
            source="decisions",
            captured=f"2026-09-0{day}T00:00:00Z",
            records=[Record(id="retention", title="Retention decision", text=decision, aliases=["decision:retention"])],
        )
        store.write(f"records/{day}.json", encode(capture.model_dump()))
    store.write(
        "records/undated.json",
        encode(
            Collection(
                source="undated", captured="2026-09-01T00:00:00Z", records=[Record(id="one", title="Undated capture")]
            ).model_dump()
        ),
    )
    if state != "missing":
        build(store)
    if state == "corrupt":
        store.write(CACHE, b"broken")
    report = evaluate(store)
    assert report["passed"], report
