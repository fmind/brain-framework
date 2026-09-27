# Install and update

## Install

```bash
uv tool install brain-framework
bf --version
```

See [Getting started](getting-started.md) to create your brain and collect your first record.

## Update

Read the [release notes](https://github.com/fmind/brain-framework/releases), keep a backup, then update your installation:

```bash
uv tool upgrade brain-framework
cd ~/brain
bf --version
bf validate
bf eval
```

Use the same release across a team's clones and collectors. Restart connected agent hosts and watchers after updating. If validation fails, inspect its diagnostics before resuming collection; [safeguards](limits.md) covers recovery.

## From 12 to 13

Version 13 is a breaking release. Brain configuration moves from format 5 to **6**; retrieval evaluation files stay at format **5**. Do not change only the configuration version: monthly JSONL memories must become independent JSON records first. Existing `source:id` references remain stable.

1. Stop watchers, timers, editors and other writers. With version 12, run `bf build` to recover any interrupted transaction, then `bf validate`. Keep a private backup of the **whole brain**, including ignored memories and action attachments, outside its Git checkout. Git alone is not that backup.
1. Work on a full copy of that backup. Install version 13 in a separate environment so the original brain remains usable with version 12. Do not run collection yet.
1. Convert each nonblank line of `memories/SOURCE/*.jsonl` into `memories/SOURCE/SHA256_ID.json`, where `SHA256_ID` is the lowercase SHA-256 of the record's UTF-8 `id`. Preserve every record field. Reject duplicate IDs within a source rather than picking a revision. The example below writes a separate directory and leaves the old files untouched.
1. In the copied brain, replace `memories/` with the converted `memories/` directory. Keep the originals in the external backup. Set `version: 6` in `bf.yaml` and remove sensor `trust` fields. Review each configured program: registration no longer grants execution permission; explicit `collect`, `update` and `watch` run enabled programs in selected roots.
1. Give projects, concepts and canonical action `ACTION.md` notes a nonempty `type` and `status: draft`, `stable` or `deprecated`. Move work states such as active/done into the body and checkboxes. Keep structured `sources` and verification metadata consistent with [OKF](concepts.md). Replace aliases that claim another brain's namespace; only explicit links may cross that boundary.
1. Back up the optional user registry, then remove each registration's `collect` field and obsolete `--collect` flags from host commands. Registration now contains only `path`. Update separately installed [skills and agent connections](agents.md). Scripts consuming replies must follow `next_offset`, reassemble digest-checked exact-read chunks, and stop depending on removed `external`, `trust`, `collect` or source `partitions` fields. Routine action paths now include a UUID suffix.
1. With version 13, run `bf build`, `bf validate`, `bf eval` and the brain's technical tests on the copy. Search for a known decision, read its reason and supporting `source:id`, and compare record counts with the backup. Review `problems` and `stale`; source freshness is separate from a successful conversion. Only then switch all collectors and agent hosts to the upgraded brain and resume the authorized schedule.

Brains without a `memories/` directory have no collected records to convert. The following standard-library example runs from the **stopped backup copy**. It refuses symlinks, unexpected record files, duplicate JSON keys and duplicate IDs. Its output is a fresh private sibling directory; a failed run leaves a partial candidate there for inspection, never changes the original memories, and must not be installed.

```bash
python3 - <<'PY'
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


root = Path.cwd()
if not Path("memories").is_dir() or Path("memories").is_symlink() or Path("memories/.pending").exists():
    raise SystemExit("Expected recovered, regular memories directory in the stopped backup copy")
target = Path(tempfile.mkdtemp(prefix="brain-v13-records-", dir=root.parent))
(target / "memories").mkdir(mode=0o700)
count = 0
for directory, folders, files in os.walk("memories", followlinks=False):
    for name in [*folders, *files]:
        path = Path(directory, name)
        if path.is_symlink():
            raise SystemExit("Symlink found; inspect the backup before converting")
    for name in files:
        path = Path(directory, name)
        if (not stat.S_ISREG(path.stat().st_mode) or len(path.parts) != 3
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", path.parts[1])
                or not re.fullmatch(r"(?:\d{4}-\d{2}|undated|snapshot)\.jsonl", name)):
            raise SystemExit("Unexpected file; inspect the backup before converting")
        with path.open("rb") as source:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line, object_pairs_hook=unique)
                if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                    raise SystemExit("Invalid record id; inspect the backup")
                data = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
                if len(data) > 16 << 20:
                    raise SystemExit("Record exceeds 16 MiB; inspect the backup")
                destination = target / path.parent / (hashlib.sha256(record["id"].encode()).hexdigest() + ".json")
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    output.write(data)
                count += 1
print(f"Converted {count} records into {target}; originals unchanged")
PY
```

Expected result: one `.json` file per original record, the same IDs and fields, and a printed count and destination. An existing destination file means duplicate IDs or a hash collision: resolve the evidence conflict in a new copy and rerun. Do not resume collection on a partially converted brain. If any acceptance check fails, keep the original runtime and backup active; restore both together rather than feeding format-6 files to version 12.
