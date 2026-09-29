---
description: Look up size, page and process limits, and the safeguards around running programs.
---

# Limits and safeguards

Look up exact limits for a skipped file, a partial reply or a stopped program. For recovery steps, start with [Troubleshooting](troubleshooting.md); for what to review before collecting or sharing, see [Privacy](privacy.md).

## Size bounds

| Resource                                      | Limit                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------ |
| `bf.yaml`, machine registry, eval suite       | 1 MiB each                                                                     |
| YAML structure, including frontmatter         | 32 nesting levels and 20,000 parser events                                     |
| Authored note                                 | 4 MiB                                                                          |
| Note or record title                          | 4,096 characters; listings show 200, then `…`                                  |
| Search or page reply                          | Items end early near 32 KiB and continue at `next_offset`                      |
| Exact read                                    | 32 KiB of JSON, then text pages; a large note's first page holds 4 KiB of text |
| Outline of a paged note                       | 200 sections                                                                   |
| Backlink previews                             | 5 newest items per relation, excerpts of 160 characters                        |
| Claims of a subject                           | 20 per relation and 50 in all                                                  |
| Record file                                   | 16 MiB                                                                         |
| Record fields other than `text`               | 2 MiB, so an exact read's first page always fits                               |
| Record id                                     | 1–4,096 characters and 7,988 once percent-encoded                              |
| Record text                                   | 4,194,304 characters                                                           |
| Record links, aliases                         | 1,000 each                                                                     |
| Record URL, link, field value                 | 8,192 characters                                                               |
| Listed field value                            | 200 characters; longer values stay in the exact read                           |
| Entries per record source or authored folder  | 100,000                                                                        |
| Folder depth                                  | 64 levels below a scanned folder                                               |
| Note path                                     | 7,988 characters once percent-encoded                                          |
| Search query                                  | 4,096 characters; the first 32 distinct words match                            |
| Search results per page                       | 50                                                                             |
| Page sizes                                    | Folders and tags 200; tasks, periods and relation pages 50; source pages 20    |
| Offsets                                       | 2^53−1                                                                         |
| Skipped files per brain                       | 200 listed in `problems`, then a count                                         |
| Validation problems, warnings                 | 200 each, then a `*_truncated` flag; 20 spellings per warning                  |
| Relation `targets`                            | 64 prefixes of at most 1,024 characters                                        |
| Retrieval suites                              | 100 suites per run, 200 cases per suite                                        |
| Reprojection                                  | 1,000 changed records per transaction; 200 failed records listed               |
| Sensor output                                 | 64 MiB by default, up to 256 MiB                                               |
| Routine output                                | 1 MiB by default, up to 4 MiB                                                  |
| Routine input from `bf run`                   | 1 MiB, ending within 10 seconds when piped                                     |
| Program command                               | 128 arguments of at most 16,384 characters each                                |
| Program `timeout`                             | 1–3,600 seconds                                                                |
| `refresh`, `lookback`, `overlap`, `reconcile` | up to 365 days                                                                 |
| Program log                                   | 1 MiB per `logs/NAME.log`; 256 KiB of output per run                           |

Each record takes one folder entry. `bf status` warns above 80,000 entries without failing `--check`; a tree over the limit fails the scan and names the folder. Split a crowded source's sensor or archive its older records, and keep bulky imports in `inputs/` or `originals/`, which are not scanned. A long pasted question matches only its first 32 distinct words after function words are dropped: keep the distinctive terms.

Page sizes bound one reply, not the result: follow `next_offset`. SQLite still ranks every match up to a deep offset, so very deep pages take longer.

## Processes and logs

Sensors and routines run from the brain folder with direct arguments, without a shell. Their standard input is closed, except for what `bf run` pipes to a routine. They receive `BF_BRAIN` set to that brain, so a nested `bf` call reads the brain running it.

BF removes environment variables that could load code **before your program starts**, such as `BASH_ENV` or `PYTHONPATH`:

| Removed                                                           | Reason                                                 |
| ----------------------------------------------------------------- | ------------------------------------------------------ |
| `LD_*`, `DYLD_*`, `GCONV_PATH`                                    | Can inject loader libraries or conversion modules.     |
| `BASH_ENV`, `ENV`, `BASH_FUNC_*`, `SHELLOPTS`, `BASHOPTS`, `PS4`  | Can supply shell startup code, options or functions.   |
| `PYTHON*`, Java, Node, Ruby and Perl startup options, `LUA_INIT*` | Can alter interpreter startup or module loading.       |
| Relative or empty `PATH` entries                                  | Could run an unexpected program from the brain folder. |

Other variables, including provider credentials and absolute `PATH` entries, remain. The list is best effort, not a sandbox. When a program needs dependencies, use an explicit runtime such as `uv run --no-project sensors/example.py`.

A timeout, cancellation, closed terminal or excessive output kills the program's whole process group; partial output never replaces stored records. A run ends when the program exits and its output closes. A background helper that keeps output open more than a second after the exit fails the run: redirect its output, for example to `/dev/null`. Watch and generated schedules allow 60 seconds after a stop request, so an interrupted record write can roll back.

Each program appends to `logs/NAME.log` in the brain: a `== TIME OUTCOME ==` line per run, then its stderr, and a log routine's stdout. The log keeps its newest whole entries within 1 MiB. Errors and run history name the log without quoting provider output. Search and read keep a local `usage.jsonl` of at most 1 MiB, with the time, operation and result count, never queries or refs; `bf status` summarizes 7 and 30 days.
