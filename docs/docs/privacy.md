---
icon: lucide/shield
description: Understand local storage, cloud-agent boundaries, program execution and audience separation.
---

# Privacy and security

**Brain Framework collects no telemetry and runs no hosted service — you own your files.** Notes and records stay on your machine unless you share them through Git or connect an external model or provider tool.

## What stays local

- **Notes and records**: plain Markdown and JSON files on disk.
- **Retrieval (`bf search`, `bf read`, `bf mcp`, `bf export`)**: strictly offline, with zero network or model calls.
- **Search cache and local state (`.bf/`)**: mode-`700` local SQLite and runtime folder (`.bf/cache.sqlite3`, `.bf/locks/`, `.bf/state/`, `.bf/runtime.sqlite3`, `.bf/usage.sqlite3`). Exclude `.bf/` from cloud sync, and protect `.bf/runtime.sqlite3` as sensitive state (note `.bf/usage.sqlite3` keeps only aggregate counts and up to 8 unmatched tokens on zero-hit searches — never query strings, refs, paths or identities).
- **Program logs (`logs/`)**: bounded stderr logs (gitignored and unindexed); keep private because they may hold provider errors.

## Your agent has its own privacy rules

Connecting a cloud agent harness sends retrieved text and prompts to that harness's model provider:

- **Claude Code**: review Anthropic's [data usage guide](https://code.claude.com/docs/en/data-usage) and `.claude/settings.json` hooks/permissions.
- **Codex**: review [Codex security](https://developers.openai.com/codex/security/) and [OpenAI API data controls](https://platform.openai.com/docs/guides/your-data).
- **Fully local option**: pair BF's CLI or MCP server with a locally hosted model, encrypt disk/backups and use local-file sensors.

## Trust boundaries

| Party                    | Influence                              | Enforced by BF                                                                                                                                 | Enforced by you                                                                                                    |
| ------------------------ | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Owner                    | Configured jobs and selected brain     | Subprocesses run only from explicit execution commands in one owner-owned brain, without a shell and with loader vars stripped.                | Reviewing `run`, `harness`, `send` and `gate` commands and permissions.                                            |
| Channel senders          | Message text in `{{context}}`          | Remote text never selects commands or output routes; queued deliveries pin their send configuration.                                           | Sender authentication and destination bounds in message jobs ([choose inputs](prompts.md#choose-inputs)).          |
| Teammates via Git        | Pulled notes, records and scripts      | Retrieval and registration never execute code.                                                                                                 | Review pulled changes before running jobs; copy hooks/skills outside shared clones.                                |
| Provider content         | Record text and attributes             | Replies include an untrusted-evidence `notice` and escape control/bidi format characters as `\uXXXX`.                                          | Treating retrieved text as evidence; restricting harness tools ([propose, then act](prompts.md#propose-then-act)). |
| Agent harness / MCP host | Tool calls and outbound model requests | `bf mcp` exposes only read-only `search` and `read` over local stdio; read-only triage can emit `draft` actions via `bf action new` or stdout. | Harness tool allowlists, sandboxes and provider retention settings.                                                |

To prevent prompt injection from exfiltrating private context, avoid combining private data, untrusted inputs and outbound/write tools in one run: keep scheduled triage read-only (writing `draft` proposals via `emit: action` or `bf action new`) and reply only on owner-authenticated channels.

## Running code

| Command                                                                                                                                 | Executes                                                                                                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bf search`, `bf read`, `bf mcp`, `bf export`, `bf doctor`, `bf update --dry-run`                                                       | Nothing (offline read/plan/check only).                                                                                                                   |
| `init`, `register`, `unregister`, `skill install`, `guide`, `action`, `schema`, `status`, `validate`, `eval`, `build`, `forget`, `wake` | Nothing (local file/cache/queue operations only).                                                                                                         |
| `bf collect JOB` (including `--dry-run`)                                                                                                | The named record job (`bf collect --input FILE` runs no job).                                                                                             |
| `bf run JOB` (including `--dry-run`), `bf run --hook EVENT`                                                                             | The named job (`run`, `harness`, `send`, or `gate`) or every job matching that hook.                                                                      |
| `bf post`, `bf queue DELIVERY_ID --retry`                                                                                               | The configured `send:` job.                                                                                                                               |
| `bf update`, `bf watch`                                                                                                                 | Due record jobs, scheduled/event jobs, wakes and deliveries (`watch` also keeps `daemon` jobs running).                                                   |
| `uv run --project BRAIN --locked bf ...`                                                                                                | The brain's pinned Python, `uv.lock` packages and `.venv/` ([pin runtime](upgrades.md#pin-a-brains-runtime)). Only run inside registered, audited brains. |

Before running execution commands in a shared or downloaded brain, review `bf.yaml`, `sensors/`, `routines/`, `prompts/`, `channels/`, `.git/hooks`, `.env`, `pyproject.toml`/`uv.lock` (never run a tracked `.venv/`), `.claude/` and `.mcp.json`.

## Remove collected evidence and separate audiences

- Narrow source scope first, drop single records permanently with `bf forget SOURCE:ID`, or set `expire_after` on `emit: records` jobs ([keep, forget and expire](sensors.md#keep-forget-and-expire-records)) — then purge Git history and backups separately if needed.
- Keep personal and team brains in separate repositories and protect `.bf/runtime.sqlite3` (`.bf/runtime.sqlite3` holds recent messages, events, wakes and deliveries for up to 35 days; `bf queue ID` reveals one item). Only run `uv run --locked bf ...` inside a registered brain (`bf status` shows `"registered": true`).

Report suspected security vulnerabilities privately via [SECURITY.md](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
