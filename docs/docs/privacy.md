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
- **Assistant runs:** BF never calls a model. An [assistant](prompts.md) starts your harness, which sends what the prompt and its tools read to its provider. Each run's reply and input stay in the brain's ignored `memories/`. A [`{{prompt}}` argument](prompts.md#pass-the-prompt-to-the-harness) shows the expanded prompt, input included, to local process listings while the harness runs.
- **Run history, locks and usage counts:** private [local state](configuration.md#local-state). Usage keeps times, operations, fixed [kind words](limits.md#processes-and-logs) for the scope or page, result counts and (for zero-result searches) up to 8 unmatched query tokens, never query text, refs, paths or identities.
- **Search cache:** `.bf/` holds a copy of searchable text, private to your account (mode 700). It belongs to the machine that built it: exclude it from shared archives and synced folders. BF discards and rebuilds from the files a cache it did not create in place, such as one copied or restored, or one holding triggers or views.

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

## Trust boundaries

BF enforces a few boundaries in code. Your adapters, your agent harness and your reviews enforce the rest. [Running code](#running-code) lists what each command executes.

| Who                        | What they can influence                                                                                  | What BF enforces                                                                                                                                                                                                                         | What you enforce                                                                                                    |
| -------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| You, the owner             | Which programs run, in which brain, with your account's permissions.                                     | Programs run only from [execution commands](concepts.md#what-bf-does-not-do), in the one brain you select. The working directory selects only a brain whose folder and `bf.yaml` you own ([selection](configuration.md#select-a-brain)). | What each program, adapter and harness you configure may do.                                                        |
| Admitted senders           | The message text assistants read.                                                                        | Remote text never selects commands or destinations. Queued work keeps the [configuration it was queued with](channels.md#configuration-changes).                                                                                         | Sender authentication and admission in each channel adapter ([keep prompts safe](prompts.md#keep-prompts-safe)).    |
| Teammates, through Git     | Notes, records and every program a pull brings.                                                          | Search, read, registration and references never run programs. Programs start from argv without a shell, with [startup-injection variables](limits.md#processes-and-logs) removed.                                                        | [Review before you merge](team.md#review-before-you-merge), and install hooks and skills only from reviewed copies. |
| Provider content           | Record text and fields that searches, reads and assistants return.                                       | Every search and read reply carries a [`notice`](retrieval.md#notes-records-and-identities): retrieved content is evidence, never instructions. Output escapes control and format characters.                                            | Collect only what you need, and check what it claims before acting on it.                                           |
| Your harness and its model | Whatever the harness's tools allow, including what it sends out.                                         | BF never runs a model. It starts the harness from the configured argv, within its [timeout and output bound](prompts.md#assistant-settings).                                                                                             | The harness's permissions and sandbox, and its [provider's data rules](#your-agent-has-its-own-privacy-rules).      |
| MCP hosts                  | The selected brain and its direct `brains:` references, or every registered brain when none is selected. | Two read-only tools, `search` and `read`, over local stdio, with no collection, program, write or network tool ([MCP](mcp.md#what-the-host-can-read)).                                                                                   | Which brain the host serves, and the host's other tools.                                                            |

The main risk of an assistant that acts on its own is holding private data, untrusted content and a way to send data out at once. An assistant reads the private brain, and collected mail or issues are untrusted, so remove the way out: [choose its inputs](prompts.md#choose-inputs) so its replies reach only people who may read the brain, and limit its harness's tools so a steered run cannot send data anywhere else ([keep prompts safe](prompts.md#keep-prompts-safe), [propose, then act](prompts.md#propose-then-act)).

## Running code

| Command                                                              | What it can execute                                                                                                                                                |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bf search`, `bf read`, `bf mcp`, `bf export`                        | Nothing: retrieval only.                                                                                                                                           |
| `bf update --dry-run`                                                | Nothing: a plan.                                                                                                                                                   |
| `bf collect SENSOR`, including `--dry-run`                           | The named sensor, which may contact its provider.                                                                                                                  |
| `bf run ROUTINE`, including `--dry-run`, `bf run --hook EVENT`       | The named routine, or the routines listing that hook.                                                                                                              |
| `bf run ASSISTANT`, including `--dry-run`                            | The named [assistant](prompts.md)'s harness command, with every tool and permission that command grants.                                                           |
| `bf update`, `bf watch`                                              | Due sensors, routines, assistants and channel reads/deliveries of the selected brain; watch also runs channel streams.                                             |
| A brain's pinned runtime: `uv run --project BRAIN --locked bf ...`   | The packages its `uv.lock` names, downloaded if needed, the interpreter its `.python-version` names, anything already in its `.venv/` and the `bf` that then runs. |
| `bf channel read`, `bf channel send`, `bf queue DELIVERY_ID --retry` | The configured channel read or send command.                                                                                                                       |

Every other command, including `init`, `register`, `skills`, `schema`, `status`, `validate`, `eval`, `build` and `forget`, executes nothing.

Programs run in the one brain you select with `--brain`, `BF_BRAIN` or your working directory, never in every registered brain or a referenced one. The working directory selects a brain only when you own its folder and `bf.yaml`, so another account's `bf.yaml` above a shared folder is refused.

A downloaded or shared brain's programs run with your permissions and credentials, from the brain's folder. Review before running one:

- **Programs:** `bf.yaml`, `sensors/`, `routines/`, `prompts/`, `channels/` and any Git hooks.
- **`.env` files:** a provider CLI such as `gws` loads a `.env` from the brain's folder, which can switch its account.
- **A [pinned runtime](upgrades.md#pin-a-brains-runtime):** it also runs with your permissions; review its files and shipped `.venv/` as that section says. Search and read the brain with your installed `bf`, never through its pin.
- **Agent configuration:** an assistant's harness starts in the brain's folder and loads its agent configuration. Review Claude Code's `.claude/`, whose `settings.json` hooks `--print` runs without a trust dialog, `.mcp.json` and changes to `AGENTS.md` or `CLAUDE.md`.
- **Agent host hooks**, such as `hooks/`: copy them into a folder only you write, and never register one from a brain others can push to. The host runs a registered hook at every session or prompt, so a pulled change would run unreviewed.

Replies and diagnostics escape control characters and invisible format characters, such as bidi controls, as `\uXXXX`. Retrieved text therefore cannot drive your terminal through BF's output, but a tool that decodes the JSON, such as `jq -r`, prints them raw. A retrieved email saying “run this command” is evidence to assess, never authority to act.

### Channels and the assistant queue

- Channels execute only through explicit channel commands or authorized assistant and watch work.
- Adapters own authentication, admitted senders and fixed destinations. Remote text never selects executable commands.
- Queued inputs, saved answers and deliveries hold private content in the local BF runtime state. Preserve it for recovery and protect it like the brain.
- `bf queue ID` intentionally reveals that item's private body.
- Only the newest 8 MiB of finished work keeps its bodies, and finished jobs and deliveries are deleted 35 days later; see [queue limits](limits.md#processes-and-logs).
- Search, read and status open the queue read-only and never create it.

See [channel recovery](channels.md#queue-and-recovery).

## Remove collected evidence

Collect less first: narrow a sensor's accounts, folders and fields, since what it never prints never reaches the brain. Then:

- `bf forget SOURCE:ID` removes one record and keeps its sensor from storing it again; see [keep, forget and expire records](sensors.md#keep-forget-and-expire-records).
- `expire_after` on a sensor removes its records once they are older than the retention you choose.
- Both act on the brain's files only. Copies stay in Git history, backups, exports and any agent session that read them; rewrite or expire those separately.

## Separating audiences

- Keep personal and team knowledge in separate private brains.
- Review direct `brains:` references: they add readable evidence to every search.
- Share only files every recipient may read and retain; Git history keeps committed content.
- Keep credentials with provider tools, and review sensor output before saving or sharing it.
- Back up notes, records, originals, any pending recovery journal and the local assistant queue: BF is not a backup.

See [team setup](team.md) for sharing, [What BF does not do](concepts.md#what-bf-does-not-do) for its boundaries and [limits](limits.md#processes-and-logs) for process safeguards. Report suspected vulnerabilities through [private reporting](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
