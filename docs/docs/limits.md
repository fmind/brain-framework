---
description: Look up file, process and reply limits and the safeguards that preserve saved evidence.
---

# Limits and safeguards

Use this reference to look up exact limits for a skipped file, partial reply or stopped program. For step-by-step recovery, start with [Troubleshooting](troubleshooting.md). The [privacy guide](privacy.md) explains what to review before collecting or sharing evidence.

## Files

| If you see…                                                       | What to do                                                                             |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| A skipped symlink or special file in `problems`                   | Use a regular file inside the brain. BF never follows the skipped target.              |
| `file name is not valid UTF-8 or contains a backslash; rename it` | Rename the file shown with `\xNN` escapes, such as a Latin-1 name from an old archive. |
| An interrupted record transaction                                 | Keep `memories/.pending/`, then run `bf build` and `bf validate`.                      |
| A busy writer or stale cache                                      | Let the writer finish, then repeat the read.                                           |

Writes are atomic. Processes using the same physical brain share its writer lock and each program's lock, even through another path such as a bind mount; run history follows the resolved path. Use the same [state directory](configuration.md#local-state) for those processes. Back up an idle brain, including any pending journal. These protections are not a sandbox against other programs running as your account.

Linked or special folders are named in errors, such as `settings: expected a directory; symlinks and special files are not followed`; replace them with regular directories. A regular file where a path needs a folder cannot hold that path: writing below it fails with `expected a directory, found a file`, and an exact read finds the note in another selected brain or reports it not found. A file removed while a scan runs, as by an editor's atomic save, is skipped until the next refresh. Emacs lock files beside authored files, named `.#NAME` and often dangling links, are ignored in `projects/`, `concepts/` and `actions/`: an open editor never fails a build or update. A name that is not valid UTF-8, or that holds a backslash, cannot become a ref: it is reported like a link, shown with `\xNN` escapes. A snapshot collection of a source folder holding one fails until it is renamed; window collections proceed, while `bf build`, `bf update` and `bf validate` keep reporting it.

## Size bounds

| Resource                                | Limit                                                           |
| --------------------------------------- | --------------------------------------------------------------- |
| `bf.yaml`, machine registry, eval suite | 1 MiB each                                                      |
| `settings/watch.yaml`                   | 64 KiB                                                          |
| YAML structure, including frontmatter   | 32 nesting levels and 20,000 parser events                      |
| Authored note                           | 4 MiB                                                           |
| Note title                              | 4,096 characters                                                |
| Search or page reply                    | 4 MiB; items end early near 2 MiB and continue at `next_offset` |
| Exact read before chunks                | 65,536 Unicode characters of serialized JSON                    |
| Exact-read chunk                        | 65,536 Unicode characters                                       |
| Record file                             | 16 MiB                                                          |
| Entries per scanned tree                | 100,000                                                         |
| Directory depth                         | 64 levels below a scanned folder                                |
| Record id                               | 1–4,096 characters and 7,988 once percent-encoded               |
| Note path                               | 7,988 characters once percent-encoded                           |
| Record title                            | 1–4,096 characters                                              |
| Record text                             | 4,194,304 characters (4 Mi)                                     |
| Record links, aliases                   | 1,000 each                                                      |
| Record URL                              | 8,192 characters                                                |
| Search query                            | 4,096 characters; the first 32 distinct words are matched       |
| Search results per page                 | 50                                                              |
| Skipped files per brain                 | 200 listed in `problems`, then a count                          |
| Period listing                          | 50 items per page                                               |
| Folder listing                          | 200 notes per page                                              |
| Source overview                         | 20 records per page                                             |
| Retrieval suites                        | 100 suites per run, 200 cases per suite                         |
| Sensor stdout                           | 64 MiB by default; configurable up to 256 MiB                   |
| Routine stdout                          | 1 MiB by default; configurable up to 4 MiB                      |
| Program command                         | 128 arguments of at most 16,384 characters each                 |
| Program `timeout`                       | 1–3,600 seconds                                                 |
| Program `refresh`, `lookback`           | up to 31,536,000 seconds (365 days)                             |
| Sensor `overlap`, `reconcile` values    | up to 31,536,000 seconds (365 days)                             |

Each record consumes one filesystem entry. The 100,000-entry limit applies to the entire recursive traversal of `memories/`, across sources, including directories and ignored extensions. Allow room for source directories when sizing a corpus. A tree over either scan bound fails the scan with an error naming the directory; move deep or bulky trees to root `inputs/` or `originals/`.

Search drops function words before counting query words, so a long pasted question matches only its first 32 distinct remaining words; shorten it to the distinctive terms.

Keep bulky imports in root `inputs/` or `originals/`, which are not scanned. Link an authored note to the retained file and record the useful conclusion in the note.

Page sizes do not limit the whole result set. For example, a period with 51 items needs a second request after its first 50. Follow the returned `next_offset`; listings also report `total`. Summary sections remain bounded previews. Large exact reads use lossless JSON chunks with a digest; see [continuations](retrieval.md#continuations).

Pagination retains only the requested page in Python, but SQLite still ranks every match up to the offset. Deep offsets can take longer; excerpts and relations are computed only for returned results.

## Processes and logs

Sensors and routines run from the brain root with stdin closed and direct arguments, without a shell. They receive `BF_BRAIN` set to that brain, so a nested `bf` call without `--brain` reads the brain running it, never a selection inherited from your shell.

### Why remove environment variables?

Some inherited variables can load code **before your sensor starts**. For example, `BASH_ENV` tells Bash to read a startup script, and `PYTHONPATH` can replace the modules a Python sensor imports. BF removes these known startup variables so a sensor starts from its declared command. The list is best effort, not a sandbox: anyone who controls your environment can still change what a command on `PATH` runs.

| Removed                                                          | Reason                                                          |
| ---------------------------------------------------------------- | --------------------------------------------------------------- |
| `LD_*`, `DYLD_*`, `GCONV_PATH`                                   | Can inject loader libraries or conversion modules.              |
| `BASH_ENV`, `ENV`, `BASH_FUNC_*`, `SHELLOPTS`, `BASHOPTS`, `PS4` | Can supply shell startup code, options or imported functions.   |
| `PYTHON*`, Java/Node/Ruby/Perl startup options, `LUA_INIT*`      | Can alter interpreter startup or module loading.                |
| Relative or empty `PATH` entries                                 | Could select an unexpected executable from the brain directory. |

Other variables, including ordinary provider credentials and absolute `PATH` entries, remain available. If a sensor needs dependencies, use an explicit runtime such as `uv run --no-project sensors/example.py`; do not depend on inherited interpreter startup settings.

Timeout, SIGTERM, a closed terminal, cancellation or excessive output kills the process group. The shipped provider examples also bound subprocess output while reading it. For example, a sensor that exceeds its configured output limit fails collection; its partial stdout does not replace the stored records.

A run ends when the program exits and its stdout closes. A background helper it started, such as an SSH connection master, may keep stderr open: BF keeps the stderr written so far and ends the helper with the process group. A helper that keeps stdout open for more than a second after the program exits fails the run with `a background process kept its stdout open`, because the output may be incomplete; redirect that helper's output, for example to `/dev/null`.

Stderr goes to a private log capped at 256 KiB. Errors name that log without quoting provider output; validation errors identify record positions and field names. Provider-controlled keys never appear in errors or run history.

Search and read keep a local `usage.jsonl`, capped at 1 MiB, with the time, operation and result count. `bf status` summarizes 7 and 30 days. Evaluation calls do not count as usage.

## Evidence limits

A cache rebuilt today can contain mail last collected a month ago. Check both cache state and source coverage before calling the evidence current.

An empty result with `problems` or `stale` cannot establish absence. Brain Framework validates structure and links; people and agents verify claims. See [incomplete answers and freshness](retrieval.md#incomplete-answers-and-freshness) for the fields to inspect.
