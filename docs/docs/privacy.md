---
icon: lucide/shield
description: Understand local storage, cloud-agent boundaries, program execution and audience separation.
---

# Privacy and security

**Brain Framework collects no telemetry and runs no hosted service — you own your files.** Notes and records stay on your machine unless you share them through Git or connect an external model or provider tool.

## What stays local

- **Notes and records**: plain Markdown and JSON files on disk.
- **Retrieval (`bf search`, `bf read`, `bf mcp`, `bf export`)**: strictly offline, with zero network or model calls.
- **Search cache (`.bf/`)**: mode-`700` disposable SQLite cache bound to the local machine; exclude it from backups and cloud sync.
- **Program logs (`logs/`)**: bounded stderr logs (gitignored and unindexed); keep private because they may hold provider errors.
- **Local state (`$XDG_STATE_HOME/bf/`)**: locks, run timestamps, durable assistant queues and aggregate usage counts (operations, result counts and up to 8 unmatched tokens on zero-hit searches — never query strings, refs, paths or identities).

## Your agent has its own privacy rules

Connecting a cloud agent harness sends retrieved text and prompts to that harness's model provider:

- **Claude Code**: review Anthropic's [data usage guide](https://code.claude.com/docs/en/data-usage) and `.claude/settings.json` hooks/permissions.
- **Codex**: review [Codex security](https://developers.openai.com/codex/security/) and [OpenAI API data controls](https://platform.openai.com/docs/guides/your-data).
- **Fully local option**: pair BF's CLI or MCP server with a locally hosted model, encrypt disk/backups and use local-file sensors.

## Trust boundaries

| Party                    | Influence                                 | Enforced by BF                                                                                                                                | Enforced by you                                                                                                    |
| ------------------------ | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Owner                    | Configured commands and selected brain    | Subprocesses run only from explicit execution commands in one owner-owned brain, without a shell and with loader vars stripped.               | Reviewing sensor, routine, channel and harness commands and permissions.                                           |
| Channel senders          | Message text in `{{input}}`/`{{context}}` | Remote text never selects commands or output routes; queued jobs pin their configuration snapshot.                                            | Sender authentication and destination bounds in channel adapters ([choose inputs](prompts.md#choose-inputs)).      |
| Teammates via Git        | Pulled notes, records and scripts         | Retrieval and registration never execute code.                                                                                                | [Review before you merge](team.md#review-before-you-merge); copy hooks/skills outside shared clones.               |
| Provider content         | Record text and fields                    | Replies include an untrusted-evidence `notice` and escape control/bidi format characters as `\uXXXX`.                                         | Treating retrieved text as evidence; restricting harness tools ([propose, then act](prompts.md#propose-then-act)). |
| Agent harness / MCP host | Tool calls and outbound model requests    | `bf mcp` exposes only read-only `search` and `read` over local stdio; `propose: true` validates `draft` actions without granting write tools. | Harness tool allowlists, sandboxes and provider retention settings.                                                |

To prevent prompt injection from exfiltrating private context, avoid combining private data, untrusted inputs and outbound/write tools in one run: keep scheduled triage read-only (using `propose: true` for action drafts) and reply only on owner-authenticated channels.

## Running code

| Command                                                                                           | Executes                                                                                                      |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `bf search`, `bf read`, `bf mcp`, `bf export`, `bf update --dry-run`                              | Nothing (offline read/plan only).                                                                             |
| `init`, `register`, `skills`, `action`, `schema`, `status`, `validate`, `eval`, `build`, `forget` | Nothing (local file/cache operations only).                                                                   |
| `bf collect SENSOR` (including `--dry-run`)                                                       | The named sensor (`bf collect --input FILE` runs no sensor).                                                  |
| `bf run ROUTINE` (including `--dry-run`), `bf run --hook EVENT`                                   | The named routine or matching hook routines.                                                                  |
| `bf run ASSISTANT` (including `--dry-run`)                                                        | The assistant's harness command with all permissions its argv grants.                                         |
| `bf update`, `bf watch`                                                                           | Due sensors, routines, assistants and channel reads/sends (`watch` also runs streams).                        |
| `bf channel read`, `bf channel send`, `bf queue DELIVERY_ID --retry`                              | The configured channel capability command.                                                                    |
| `uv run --project BRAIN --locked bf ...`                                                          | The brain's pinned Python, `uv.lock` packages and `.venv/` ([pin runtime](upgrades.md#pin-a-brains-runtime)). |

Before running execution commands in a shared or downloaded brain, review `bf.yaml`, `sensors/`, `routines/`, `prompts/`, `channels/`, `.git/hooks`, `.env`, `pyproject.toml`/`uv.lock` (never run a tracked `.venv/`), `.claude/` and `.mcp.json`.

## Remove collected evidence and separate audiences

- Narrow sensor scope first, drop single records permanently with `bf forget SOURCE:ID`, or set `expire_after` on sensors ([keep, forget and expire](sensors.md#keep-forget-and-expire-records)) — then purge Git history and backups separately if needed.
- Keep personal and team brains in separate repositories, audit `brains:` references, and protect `$XDG_STATE_HOME/bf/runtime/` (`queue.sqlite3` holds recent message/answer bodies for up to 35 days; `bf queue ID` reveals one item).

Report suspected security vulnerabilities privately via [SECURITY.md](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
