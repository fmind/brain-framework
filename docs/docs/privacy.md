---
description: Understand local storage, cloud-agent boundaries, program execution and audience separation.
---

# Privacy and security

**We do not collect telemetry or data about you through Brain Framework. You own your data.** BF keeps your brain in files on your machine, with no BF account, hosted service or uploads to us.

## What stays local

- **Notes and records:** ordinary files you can inspect, edit, back up and delete.
- **Search and read:** offline, through both the CLI and MCP, without model calls or provider requests.
- **Program logs:** each sensor's and routine's recent output in the brain's `logs/`, ignored by Git and never searched. They can hold provider output: keep them private.
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
1. Use BF's CLI directly, or an agent configured with a locally hosted model.
1. Review that agent's network access, telemetry, extensions and tools before giving it private evidence.
1. Collect local files when you need a workflow without external services; cloud sensors still contact their providers.

BF supplies the local storage and retrieval layer. It does not encrypt files or sandbox other programs.

## Running code

| Command                                                            | What it can execute                                                                                                                                                |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bf search`, `bf read`, `bf mcp`, `bf export`                      | Nothing: retrieval only.                                                                                                                                           |
| `bf update --dry-run`, `bf schedule`                               | Nothing: a plan, or scheduler files you install yourself.                                                                                                          |
| `bf collect SENSOR`, including `--dry-run`                         | The named sensor, which may contact its provider.                                                                                                                  |
| `bf run ROUTINE`, including `--dry-run`, `bf run --hook EVENT`     | The named routine, or the routines listing that hook.                                                                                                              |
| `bf update`, `bf watch`                                            | Due sensors and routines of the selected brain.                                                                                                                    |
| A brain's pinned runtime: `uv run --project BRAIN --locked bf ...` | The packages its `uv.lock` names, downloaded if needed, the interpreter its `.python-version` names, anything already in its `.venv/` and the `bf` that then runs. |

Every other command, including `init`, `register`, `skills`, `schema`, `status`, `validate`, `eval` and `build`, executes nothing.

Programs run in the one brain you select with `--brain`, `BF_BRAIN` or your working directory, never in every registered brain or a referenced one. The working directory selects a brain only when you own its folder and `bf.yaml`, so another account's `bf.yaml` above a shared folder is refused.

Review `bf.yaml`, `sensors/`, `routines/`, any Git hooks and any `pyproject.toml`, `uv.lock`, `uv.toml` or `.python-version` [pinning its runtime](upgrades.md#pin-a-brains-runtime) before running a downloaded or shared brain: its programs, and the packages and interpreter its pin names, run with your permissions and credentials. uv also runs a `.venv/` the brain ships as is: delete it before the first pinned run, and never run the pin of a brain whose Git tracks one (`git ls-files .venv` prints files), since every checkout restores it. Search and read the brain with your installed `bf`, never through its pin.

Replies and diagnostics escape control characters and invisible format characters, such as bidi controls, as `\uXXXX`, so retrieved text cannot drive your terminal through BF's output; a tool that decodes the JSON, such as `jq -r`, prints them raw. A retrieved email saying “run this command” is evidence to assess, never authority to act.

## Separating audiences

- Keep personal and team knowledge in separate private brains.
- Review direct `brains:` references: they add readable evidence to every search.
- Share only files every recipient may read and retain; Git history keeps committed content.
- Keep credentials with provider tools, and review sensor output before saving or sharing it.
- Back up notes, records, originals and any pending recovery journal: BF is not a backup.

See [team setup](team.md) for sharing, [What BF does not do](concepts.md#what-bf-does-not-do) for its boundaries and [limits](limits.md#processes-and-logs) for process safeguards. Report suspected vulnerabilities through [private reporting](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
