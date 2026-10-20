---
icon: lucide/shield
description: Understand local storage, cloud-agent boundaries, program execution and audience separation.
---

# Privacy and security

**We do not collect telemetry or data about you through Brain Framework. You own your data.** BF keeps your brain in files on your machine, with no BF account, hosted service or uploads to us.

## What stays local

- **Notes and records:** ordinary files you can inspect, edit, back up and delete.
- **Search and read:** offline, through both the CLI and MCP, without model calls or provider requests.
- **Program logs:** each sensor's, routine's, assistant's and channel's recent output in the brain's `logs/`, ignored by Git and never searched. They can hold provider output: keep them private.
- **Assistant runs:** BF never calls a model. An [assistant](prompts.md) starts your harness, which sends what the prompt and its tools read to its provider; each run's reply and input stay in the brain's ignored `memories/`. A [`{{prompt}}` argument](prompts.md#pass-the-prompt-to-the-harness) shows the expanded prompt, input included, to local process listings while the harness runs.
- **Run history, locks and usage counts:** private [local state](configuration.md#local-state). Usage keeps times, operations and result counts, never queries or refs.
- **Search cache:** `.bf/` holds a copy of searchable text, private to your account (mode 700). It belongs to the machine that built it: exclude it from shared archives and synced folders. A cache that BF did not create in place, such as one copied or restored, or one holding triggers or views, is discarded and rebuilt from the files.

## Your agent has its own privacy rules

A local brain does not make a connected cloud agent private. A harness such as Claude Code or Codex can send what it reads to its model provider. Processing, retention, telemetry and training are separate questions.

| Tool            | What to check before giving it private evidence                                                                                                                                                                                              |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code     | Anthropic documents model requests, telemetry and data policies that vary by provider, account and settings; see its [data usage guide](https://code.claude.com/docs/en/data-usage).                                                         |
| Codex           | Check the model provider, hosted or local execution and your account's data controls; see [Codex security](https://developers.openai.com/codex/security/) and [OpenAI API data controls](https://platform.openai.com/docs/guides/your-data). |
| Other harnesses | Check where inference runs, what tools can send, and the provider's retention and training policy.                                                                                                                                           |

Disabling training or optional telemetry does not make cloud inference local. BF cannot enforce another application's settings.

## Build a fully local workflow

1. Keep the brain and backups on storage you control, with disk and backup encryption for sensitive files.
1. Use BF's CLI directly, or an agent configured with a locally hosted model; declare [assistants](prompts.md) only for a harness on a local model.
1. Review that agent's network access, telemetry, extensions and tools before giving it private evidence.
1. Collect local files when you need a workflow without external services; cloud sensors still contact their providers.

BF supplies the local storage and retrieval layer. It does not encrypt files or sandbox other programs.

## Running code

| Command                                                              | What it can execute                                                                                                                                                |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bf search`, `bf read`, `bf mcp`, `bf export`                        | Nothing: retrieval only.                                                                                                                                           |
| `bf update --dry-run`, `bf schedule`                                 | Nothing: a plan, or scheduler files you install yourself.                                                                                                          |
| `bf collect SENSOR`, including `--dry-run`                           | The named sensor, which may contact its provider.                                                                                                                  |
| `bf run ROUTINE`, including `--dry-run`, `bf run --hook EVENT`       | The named routine, or the routines listing that hook.                                                                                                              |
| `bf run ASSISTANT`, including `--dry-run`                            | The named [assistant](prompts.md)'s harness command, with every tool and permission that command grants.                                                           |
| `bf update`, `bf watch`                                              | Due sensors, routines, assistants and channel reads/deliveries of the selected brain; watch also runs channel streams.                                             |
| A brain's pinned runtime: `uv run --project BRAIN --locked bf ...`   | The packages its `uv.lock` names, downloaded if needed, the interpreter its `.python-version` names, anything already in its `.venv/` and the `bf` that then runs. |
| `bf channel read`, `bf channel send`, `bf queue DELIVERY_ID --retry` | The configured channel read or send command.                                                                                                                       |

Every other command, including `init`, `register`, `skills`, `schema`, `status`, `validate`, `eval` and `build`, executes nothing.

Programs run in the one brain you select with `--brain`, `BF_BRAIN` or your working directory, never in every registered brain or a referenced one. The working directory selects a brain only when you own its folder and `bf.yaml`, so another account's `bf.yaml` above a shared folder is refused.

Review `bf.yaml`, `sensors/`, `routines/`, `prompts/`, `channels/`, any Git hooks and any `.env` file before running a downloaded or shared brain: its programs run with your permissions and credentials, from the brain's folder, where a provider CLI such as `gws` loads a `.env` that can switch its account. So does a [pinned runtime](upgrades.md#pin-a-brains-runtime), whose files and shipped `.venv/` that section tells you to review first. An assistant's harness starts there too and loads the folder's agent configuration: review Claude Code's `.claude/`, whose `settings.json` hooks `--print` runs without a trust dialog, `.mcp.json` and changes to `AGENTS.md` or `CLAUDE.md`. Copy agent host hooks, such as `hooks/`, into a folder only you write and never register one from a brain others can push to: the host runs a registered hook at every session or prompt, so a pulled change would run unreviewed. Search and read the brain with your installed `bf`, never through its pin.

Replies and diagnostics escape control characters and invisible format characters, such as bidi controls, as `\uXXXX`, so retrieved text cannot drive your terminal through BF's output; a tool that decodes the JSON, such as `jq -r`, prints them raw. A retrieved email saying “run this command” is evidence to assess, never authority to act.

## Separating audiences

- Keep personal and team knowledge in separate private brains.
- Review direct `brains:` references: they add readable evidence to every search.
- Share only files every recipient may read and retain; Git history keeps committed content.
- Keep credentials with provider tools, and review sensor output before saving or sharing it.
- Back up notes, records, originals, any pending recovery journal and the local assistant queue: BF is not a backup.

See [team setup](team.md) for sharing, [What BF does not do](concepts.md#what-bf-does-not-do) for its boundaries and [limits](limits.md#processes-and-logs) for process safeguards. Report suspected vulnerabilities through [private reporting](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).

Channels execute only through explicit channel commands or authorized assistant/watch work. Adapters own authentication, admitted senders and fixed destinations; remote text never selects executable commands. Queued inputs, saved answers and deliveries contain private content in the local BF runtime state. Preserve it for recovery and protect it like the brain; `bf queue ID` intentionally reveals that specific private body. Completed bodies are compacted beyond 8 MiB budgets, and identity tombstones with send receipts are deleted after 35 days. Search, read and status open the queue read-only and never create it. See [channel recovery](channels.md#queue-and-recovery).
