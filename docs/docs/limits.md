# Limits and safeguards

Use this reference to understand a skipped file, a partial reply or a stopped program. The [privacy guide](privacy.md) explains what to review before collecting or sharing evidence.

## Files

Brain access rejects symlinks and special files below the root. Skipped entries appear under `problems`; their targets are never inspected. This includes linked action inputs and entries with unrelated extensions that could hide directories. For example, a symlink in `actions/.../inputs/` is reported and skipped, even when it points to a readable local file.

Writes are atomic. Readers and writers share a lock by physical brain identity, including bind mounts and alternate paths. Processes accessing one brain must use the same private state directory.

Interrupted record writes retain originals in `memories/.pending/`. If a read reports an interrupted transaction, preserve that directory and recover before reading again:

```bash
bf build --brain ~/brain
bf validate --brain ~/brain
```

`build` recovers the transaction and rebuilds the cache; collection can also recover it. Ordinary reads request recovery without changing evidence. Keep `memories/.pending/` with the records in backups. Back up an idle brain, or hold its writer lock during a live copy.

These protections do not defend against another process with your account's permissions replacing the brain or cache. Use separate operating-system accounts for adversarial separation.

## Size bounds

| Resource                   | Limit                                         |
| -------------------------- | --------------------------------------------- |
| Configuration              | 1 MiB                                         |
| Authored note              | 4 MiB                                         |
| Ordinary retrieval reply   | 4 MiB; larger exact reads use chunks          |
| Record partition           | 256 MiB                                       |
| Entries per scanned folder | 100,000                                       |
| Search results per page    | 50                                            |
| Period listing             | 50 items per page                             |
| Folder listing             | 200 notes per page                            |
| Source overview            | 20 records per page                           |
| Exact-read chunk           | 65,536 Unicode characters                     |
| Sensor stdout              | 64 MiB by default; configurable up to 256 MiB |
| Routine stdout             | 1 MiB by default; configurable up to 4 MiB    |

Keep bulky imports in root `inputs/` or `originals/`, which are not scanned. Link an authored note to the retained file and record the useful conclusion in the note.

Page sizes do not limit the whole result set. For example, a period with 51 items needs a second request after its first 50. Follow the returned `next_offset`; listings also report `total`. Summary sections remain bounded previews. Large exact reads use lossless JSON chunks with a digest; see [continuations](retrieval.md#continuations).

Pagination retains only the requested page in Python, but SQLite still ranks matches. Deep offsets can take longer.

## Processes and logs

Sensors and routines run from the brain root with stdin closed and direct arguments. Relative `PATH` entries and startup-injection variables are removed: `LD_*`, `DYLD_*`, `BASH_ENV`, all `PYTHON*` variables, and Java, Node, Ruby, Perl and Lua startup options.

Timeout, SIGTERM, a closed terminal, cancellation or excessive output kills the process group. The shipped provider examples also bound subprocess output while reading it. For example, a sensor that exceeds its configured output limit fails collection; its partial stdout does not replace the stored records.

Stderr goes to a private log capped at 256 KiB. Errors name that log without quoting provider output; validation errors identify record positions and field names. Provider-controlled keys never appear in errors or run history.

Search and read keep a local `usage.jsonl`, capped at 1 MiB, with the time, operation and result count. `bf status` summarizes 7 and 30 days. Evaluation calls do not count as usage.

## Evidence limits

A cache rebuilt today can contain mail last collected a month ago. Check both cache state and source coverage before calling the evidence current.

An empty result with `problems` or `stale` cannot establish absence. Brain Framework validates structure and links; people and agents verify claims. See [incomplete answers and freshness](retrieval.md#incomplete-answers-and-freshness) for the fields to inspect.
