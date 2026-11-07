---
icon: lucide/life-buoy
description: Diagnose installation, retrieval, collection and file problems without losing evidence.
---

# Troubleshooting

Start inside the intended brain. These commands inspect saved files and local history, without running programs or contacting providers:

```bash
bf --version
bf validate
bf status
```

Read the file, field or program the diagnostic names before retrying. Diagnostics go to stderr and never quote your content.

## Find the right fix

| Symptom                                                          | First check                                                                    | Next step                                                                                                                               |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `bf: command not found`                                          | Did uv finish installing BF?                                                   | Run `uv tool update-shell`, open a new shell and retry `bf --version`.                                                                  |
| No brain, or the wrong one                                       | Working directory and `BF_BRAIN`.                                              | Pass `--brain ~/brain`; see [brain selection](configuration.md#select-a-brain).                                                         |
| `pass --brain PATH or run inside the brain`                      | Did `collect`, `run`, `channel`, `queue`, `update` or `watch` run outside one? | Run inside the brain or pass `--brain PATH`. Registration selects brains for retrieval only.                                            |
| `… has no bf.yaml`                                               | Does `--brain`, `BF_BRAIN` or the registry name a subfolder or parent?         | Name the brain's root folder, which holds `bf.yaml`, or create a brain with `bf init`.                                                  |
| `registered brain directory is absent …` or `bf.yaml is missing` | Did the brain move, or is its disk or share unmounted?                         | Restore it, run `bf register` at its new place or remove its entry; see [registration](configuration.md#optional-machine-registration). |
| `bf: invalid input: ARGUMENT: …`                                 | The named argument in `bf COMMAND -h`.                                         | Correct that value and retry; see [exit codes](commands.md#exit-codes-and-errors).                                                      |
| An option is unknown                                             | `bf --version` and `bf COMMAND --help`.                                        | [Match the docs to your version](upgrades.md#match-the-docs-to-your-version).                                                           |
| `bf.yaml declares version …`                                     | The brain format in `bf.yaml` and `evals/*.yaml`.                              | Apply the upgrade steps in the [changelog](../changelog.md).                                                                            |
| A configuration field is rejected                                | The named file and field, or its line and column.                              | Fix its type, spelling or duplicate key; use the [editor schema](schema.md#editor-schemas).                                             |
| `… not found` or `unknown source`, with `did you mean …?`        | The suggested name.                                                            | Read the suggestion, or search for the evidence. A missing section lists the note's sections.                                           |
| `"valid":false`                                                  | Each problem's `file` and `error`.                                             | Repair them and validate again; see [Check your brain](checks.md).                                                                      |
| Search misses a known answer                                     | The exact note or record ref, and `unmatched`.                                 | Search with words from that evidence, without a scope; see [search](search.md#search).                                                  |
| A reply has `problems` or `stale`                                | Skipped files, references or a busy writer.                                    | Resolve them, or retry after the writer finishes.                                                                                       |
| `reference exists in several brains`                             | Several selected brains hold that ref.                                         | Read the result's `uri`, a `bf://NAME/...` address.                                                                                     |
| A program fails                                                  | `bf status` and its `logs/NAME.log`.                                           | See [a program fails](#a-program-fails), or [an assistant fails](#a-prompt-fails).                                                      |
| A program never runs automatically                               | `enabled`, `refresh` and selectors.                                            | See [updates do not run](#updates-do-not-run).                                                                                          |
| `"silent":true` in `bf status`                                   | The source's `changed` and `silent_after`.                                     | Check the sensor's account, filter or query; see [silent sources](schedule.md#silent-sources).                                          |
| An assistant runs automatically                                  | Its inputs and named schedules.                                                | Remove the unwanted subscription or schedule, or set `enabled: false`. Inspect existing queued work separately.                         |
| `state directory … is inaccessible`                              | The named directory and `XDG_STATE_HOME`.                                      | Make it yours and writable, or set `XDG_STATE_HOME`; see [local state](configuration.md#local-state).                                   |
| `state directory may not contain symlinks`                       | Is `~/.local/state`, `XDG_STATE_HOME` or its `bf/` folder a link?              | Replace the link with a real folder, or set `XDG_STATE_HOME` to one; see [local state](configuration.md#local-state).                   |
| `the search cache needs SQLite 3.35.0 …`                         | The SQLite version the message names.                                          | [Reinstall BF](#reinstall-bf) on a uv-managed Python.                                                                                   |
| `reading YAML needs PyYAML built with libyaml …`                 | `bf --version` and the Python uv chose for BF.                                 | [Reinstall BF](#reinstall-bf) with a PyYAML wheel that bundles libyaml.                                                                 |
| An agent cannot connect                                          | The same read in your terminal.                                                | Check absolute paths and restart the host; see [MCP troubleshooting](mcp.md#if-the-connection-fails).                                   |
| `bf mcp needs the mcp extra`                                     | Did you install BF without the `mcp` extra?                                    | [Reinstall BF](#reinstall-bf) with the extra; see [MCP setup](mcp.md#before-connecting).                                                |

### Reinstall BF

[Install and update](upgrades.md#install) gives the reinstall command for each of these messages, with and without the `mcp` extra. Restart agent hosts afterwards.

## A program fails

Failed collection leaves saved records intact. `bf status` marks the program `"failed":true`, with its `error`, consecutive `failures` and `log`, such as `logs/calendar.log`. The log can hold provider output: read it locally and never paste it into a public issue.

Fix the cause, then rerun the program with `bf collect SENSOR` or `bf run ROUTINE`. Updates retry on their own after the failure backoff.

- **`not an executable regular file`** from `bf validate`: run `chmod +x` on the program, or put an interpreter in its `command`.
- **`is not a regular file or folder`**: a missing file or a typo. Behind an interpreter, as in `[uv, run, --no-project, sensors/brief.py]`, validation checks only that the first `sensors/`, `routines/` or `channels/` path among the arguments exists.
- **`program was killed by SIGKILL`**: another process, such as the out-of-memory killer, ended the program.
- **Testing a fix**: `bf collect SENSOR --dry-run` **executes the sensor** and may contact its provider. `bf update --dry-run` only lists due work.

A snapshot fails with `snapshot would remove N of M records` when it would empty its source, or remove more than half of it and more than 10 records. Check the account, folder and provider response. Do not delete stored records to make collection pass. When the removal is intended, accept it once with `bf collect SENSOR --allow-removal`.

## An assistant fails {#a-prompt-fails}

A failed assistant run keeps its queue state and any saved answer. Its log holds the harness's stderr.

To see what the harness receives, preview the expanded prompt:

1. Add a temporary assistant that reuses the prompt file with `command: [cat]` and no `reply`, `inputs`, `post` or `schedules`, such as `prompt-check: {prompt: prompts/personal.md, command: [cat]}`.
2. Run `bf run prompt-check --dry-run`. To fill `{{input}}`, add `--stdin` and pipe a message.
3. Read the reply's `text`: it is the expanded prompt. Nothing is recorded, queued or delivered.
4. Remove that assistant afterwards.

Do not swap the `command` or `reply` of an assistant that has pending work, even for a moment: each job keeps the settings it was queued with. See [configuration changes](channels.md#configuration-changes) for what still runs and how to recover.

Common errors and replies:

- **`assistant command must print one JSON object when reply is set`**: with `reply` set, stdout must be one JSON object. Ask the harness for a single JSON result, such as `--output-format json`, not text or a stream of JSON lines. Or remove `reply` to keep all of stdout as the message.
- **`assistant reply at /… is missing or does not match bf schema --kind assistant-reply`**: run the harness once by hand with the same options and list the members of its JSON. Then point `reply.path` at the `{say, wake}` object, such as `/structured_output` for Claude Code; see [structured replies](prompts.md#structured-replies).
- **`expanded assistant prompt exceeds 120000 bytes; shorten …`**: the [prompt size limit](limits.md#size-bounds) applies on standard input too, not only to `{{prompt}}` in an argument. Shorten the prompt file or the piped input, and let the harness read evidence with `bf read`. A queued job keeps its template, so only `bf queue ID --cancel` resolves it. A message's job blocks its conversation until then.
- **`the argument holding the prompt exceeds Linux's 128 KiB limit`**: the rest of that argument pushed it over Linux's bound. Shorten it, or drop `{{prompt}}` so the prompt arrives on standard input, if the harness reads it there.
- **`… configuration changed; restore it …`**: the job's settings no longer match. The assistant's prompt, harness or output settings, or the `send` command of a channel its job delivers to, changed since queuing, or the assistant was removed or disabled. Restore them, then run `bf queue ID --retry`, or abandon the job with `bf queue ID --cancel`.
- **`"status":"skipped"` in a `bf update` reply**: without `job`, the active watch dispatcher owns automatic work. With `job`, another process runs that job, or its `skipped` reason says it waits until `bf.yaml` loads. Inspect `bf queue` for failed or uncertain jobs. Schedule progress remains independent of direct runs.
- **`program exited with status …`, with a sign-in error in the log**: the harness is not signed in for the account and environment running BF. Sign in as that user. A [service](schedule.md#run-watch-as-a-service) or timer gets only the variables its file sets, such as `PATH`, not your shell's.
- **The reply says the agent could not run `bf read` or `bf search`**: the harness cannot ask for approval without a terminal, or its `--tools` list leaves out `Bash`. Allow those read-only commands in its command, such as Claude Code's `--allowedTools 'Bash(bf read *),Bash(bf search *)'`, or in its permission settings. Keep `Bash` in any `--tools` list.
- **`program timed out after …s`**: give the harness its own time limit below `timeout`, such as Antigravity's `--print-timeout`, and keep the prompt's task small.

Inspect `bf queue` for failed or uncertain assistant jobs:

- A saved answer resumes without rerunning the harness.
- An interrupted harness needs explicit review before `bf queue ID --retry`, because tools might already have acted.
- Delivery retry sends only the saved answer.

See [assistant settings](prompts.md) and [queue recovery](channels.md#queue-and-recovery).

### Queue and channel errors

- **`PROMPT: cannot read assistant NAME's prompt; …`**: restore the prompt file or correct `prompt` in `bf.yaml`. The assistant's schedules wait meanwhile and start once it reads.
- **`assistant queue is full; …`, `admitted N of M messages`**: unfinished work fills the [queue](channels.md#queue-and-recovery). Retry or cancel the failed and uncertain work `bf queue` lists. A read input catches up at its next poll, and a stream's provider redelivers.
- **`assistant queue lacks answer space for this job; …` or `… for this run, so it was cancelled; …`**: running jobs hold the space, and the job starts once they finish. `bf run` cancels its job instead, so run it again then.
- **`assistant NAME needs N MiB of queue space per answer, …`**: lower the assistant's `max_bytes` ([limits](limits.md#processes-and-logs)), then retry the job.
- **`assistant queue holds unresolved work admitted under this brain's previous path or name; …`**: `bf status` also reports it after a move or rename. Restore the previous path or name, resolve the work with `bf queue`, then move or rename again. A drained queue needs nothing: the next `bf queue` or execution command rebinds it. Collection still works meanwhile.
- **`assistant queue predates this release; …`**: an earlier release created this queue. `bf status` reports it too, and execution refuses it. Never delete it. Follow that upgrade's manual steps in the [changelog](../changelog.md), which resolve its work and update the queue in place. Resolving needs the previous release: reinstall it, such as `uv tool install --python 3.14 'brain-framework==19.1.0'`, drain the queue, then upgrade again.
- **`unexpected assistant queue schema; …`**: another build created this queue, such as a pre-release one, and `bf status` reports it too. Resolve its work with that build and back the queue up. Then move its `queue.sqlite3` out of the `runtime/` folder: the next execution command creates an empty queue. A queue updated by hand needs the full upgrade statement in the [changelog](../changelog.md).
- **`assistant queue holds an interrupted write; …`**: a BF process was killed while writing the queue. Run `bf queue`, or any execution command, to roll the write back. Keep `queue.sqlite3` and its `-journal` file together and delete neither.
- **`job is still running; …` or `delivery is still being sent; …`**: another process, such as `bf run`, `bf update` or watch, still runs that attempt. Let it finish or stop that process, then run the command again. An attempt whose process ended is recorded first, so `--retry` or `--cancel` then resolves it.
- **`stream frame exceeded max_bytes`**: the adapter printed a line longer than `channels.NAME.stream.max_bytes`. Raise a lowered limit, up to the 1 MiB default, or print compact UTF-8. Then restart watch: the provider redelivers the message. No valid envelope exceeds 1 MiB, so beyond it, fix the adapter's framing.
- **`channel NAME: more than 1000 messages fall within one second; …`**: a read input caught up in parts down to one second, and that second still held more than one batch. An adapter that gives a backlog one timestamp causes this. Give each message its provider time, or read the channel with a `stream` input. The parts admitted before are kept.
- **`assistants.NAME.inputs: unknown channel X`**: declare `X` under `channels:` or correct the name, here or under `post`. `channel X requires MODE` or `requires send` asks for that capability: an input channel needs `send` to answer its messages.

## Updates do not run

```bash
bf update --dry-run
bf status
```

A sensor or routine runs automatically only when it is enabled, has a nonzero `refresh`, is due and matches the selectors. [Timing and health](schedule.md#timing-and-health) gives the rule.

- The dry run's `manual` list names enabled sensors and routines with `refresh: 0`, and assistants without schedules or inputs.
- Watch must stay open, in a terminal or as a service, or a timer must be active on a running machine; see [service behavior and host lifecycle](schedule.md#run-watch-as-a-service).
- Check health on the collecting machine: a teammate's clone shares the records, not the run history.

## Recover the cache or an interrupted write

While an interrupted record write awaits recovery, `bf status` reports `"pending_transaction":true`, a `stale` or `missing` [cache](commands.md#status-sources-and-routines) and a problem naming the command to run. Let active collection finish, then:

```bash
bf build
bf validate
bf status --check
```

`bf build` recovers the interrupted write and rebuilds the cache from the files:

- Before replacing records, a write keeps hard-link backups of them in `memories/.pending/`. Keep that folder until recovery succeeds.
- Recovery restores only the files that write changed.
- `bf collect`, `bf update` and `bf watch` also recover it before writing. Search and read never do.

If `bf build` refuses the journal itself, keep what it holds:

- For `unexpected file in memories/.pending`, move the file it names out of the brain and run `bf build` again.
- For any other refusal that names `memories/.pending`, move the whole `memories/.pending` folder out of the brain. Examples are `invalid memories/.pending manifest`, `duplicate paths in memories/.pending manifest` or a missing or oversized `.before` backup. Run `bf build` and `bf validate`, then collect the source its manifest names again: each record file is whole, but that collection may be half applied.

How the rebuild behaves:

- It fills `.bf/index.sqlite.new` beside the live cache and replaces the cache when complete, so searches keep answering meanwhile.
- A rebuild killed before it finishes leaves that file. The next search, read or status removes it once no build is running.
- `bf build` and `bf forget` exit 1 when the cache's `skipped` count shows unreadable files; `bf validate` names them.
- A damaged cache is discarded and rebuilt on its own.
- An exact read that meets an active write returns within about 3 seconds, marked `stale`: retry it for the final content.

## Files

| If you see…                                                                    | What to do                                                                                                                                              |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A record file edited by hand still answers with its old text                   | Run `bf build`. Searches keep the cached copy because an edit in place leaves the folder unchanged; collection renames records into place.              |
| A skipped link or special file in `problems`                                   | Replace a link with the file or folder it points to, and a special file with a regular file, inside the brain. BF never follows links.                  |
| `unreadable folder` or `inaccessible file or folder`                           | Restore read and search permission, such as with `chmod -R u+rwX memories`, or move the folder out of the brain. The rest of the brain keeps answering. |
| `unreadable file; retried next time`                                           | A read failed, such as on a network or sync mount. The next search, read or status tries again; if it persists, check the mount or disk.                |
| A file name that is not valid UTF-8, or holds a backslash or control character | Rename the file, shown with escapes such as `\xNN`, as for a Latin-1 name from an old archive.                                                          |
| `expected a folder; replace the file with a folder`                            | A file stands where `projects/`, `concepts/`, `actions/` or `memories/` belongs. Move it out of the brain, create the folder, then run `bf validate`.   |
| `expected a directory; symlinks and special files are not followed`            | Replace the linked folder with a regular one.                                                                                                           |
| `note is not UTF-8`                                                            | Convert the named note to UTF-8.                                                                                                                        |
| Git conflict markers at the start of a line                                    | Reconcile both revisions. The note stays unsearchable until then, even inside a code block.                                                             |

A skipped record that you fix in place, or whose permissions you restore, is read again on its own.

Writes are atomic:

- A write killed before its rename, by BF or the bf-use guarded-write helper, leaves a `.write-HEX` file beside its target.
- The next write of a record source removes its own.
- `bf validate` warns about those in `projects/`, `concepts/` and `actions/` (`an interrupted write left this temporary file; delete it`). None blocks an action routine.
- An editor's lock files, named `.#NAME`, are ignored.
- A file removed during a scan, as by an editor's save, is indexed at the next refresh.

## Report a reproducible problem

Include the BF version, operating system, the failing command with private paths replaced, its exit code, and expected versus observed behavior. Reproduce it with a small fictional note or record; omit private passages, credentials and logs. Use [GitHub issues](https://github.com/fmind/brain-framework/issues) for bugs and the [private reporting instructions](https://github.com/fmind/brain-framework/blob/main/SECURITY.md) for suspected vulnerabilities.
