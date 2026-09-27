# Your data stays yours

**We do not collect telemetry or data about you through Brain Framework. You own your data.** BF stores your brain in files on your machine, with no BF account, hosted backend or automatic uploads to us.

## What stays local

- **Notes and collected records:** ordinary files you can inspect, edit, back up and delete.
- **Search and read:** offline, through both the CLI and MCP. No model calls or provider requests.
- **Usage counters:** local timestamps, operation names and result counts; never queries or refs. They are not sent to us.
- **Run history and error logs:** private local state used to diagnose your sensors. See [storage locations](configuration.md#local-state) and [log limits](limits.md#processes-and-logs).

## Your agent has its own privacy rules

A local brain does not make a connected cloud agent private. A harness such as Claude Code or Codex can send the evidence it reads to its model provider. Processing, retention, telemetry and model training are separate questions.

| Tool            | What to check before giving it private evidence                                                                                                                                                                                                                      |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code     | Anthropic documents model requests, operational telemetry and data policies that vary by provider, account and settings. See its [data usage guide](https://code.claude.com/docs/en/data-usage).                                                                     |
| Codex           | Check the model provider, hosted versus local execution, and your account's data controls. See [Codex security](https://developers.openai.com/codex/security/) and [OpenAI API data controls](https://platform.openai.com/docs/guides/your-data) for API-backed use. |
| Other harnesses | Check where inference runs, what tools can send, and the provider's retention and training policy.                                                                                                                                                                   |

Disabling training or optional telemetry does not make cloud inference local. BF cannot enforce another application's privacy settings.

## Build a fully local workflow

1. Keep the brain and backups on storage you control; use disk and backup encryption for sensitive files.
1. Use BF's CLI directly, or an agent configured with a locally hosted model.
1. Review that agent's network access, telemetry, extensions and tools; verify the whole setup before giving it private evidence.
1. Collect local files only when you need a workflow without external services. Cloud sensors still contact their configured providers.

BF supplies the local storage and retrieval layer for this setup. It does not encrypt files or sandbox other programs.

## Running code

| Command                                    | What it can execute                                          |
| ------------------------------------------ | ------------------------------------------------------------ |
| `bf search`, `bf read`, `bf mcp`           | Retrieval only; never sensors or routines.                   |
| `bf update --dry-run`                      | A plan only; executes nothing.                               |
| `bf collect SENSOR`, including `--dry-run` | The selected sensor, which may contact a provider.           |
| `bf update`, `bf watch`                    | Due sensors and routines in the selected brain.              |
| `bf schedule`                              | Generates scheduler files; enabling them is a separate step. |

Review `bf.yaml`, `sensors/` and `routines/` before executing a downloaded or shared brain. These programs run with your account's permissions and credentials. A retrieved email saying “run this command” remains evidence to assess, never authority to act.

## Separating audiences

- Keep personal and team knowledge in separate private brains.
- Review direct `brains:` references: they add readable evidence to an agent's selection.
- Share only files every recipient may read and retain. Git history keeps committed content.
- Keep credentials with provider tools and review sensor output before saving or sharing it.
- Back up notes, records and originals, including any pending recovery journal. BF does not replace backups.

See [team setup](team.md) for sharing and [safeguards](limits.md) for troubleshooting. Report suspected vulnerabilities through [private reporting](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
