# Privacy and security

Your brain is stored in plain files on your machine. Brain Framework does not upload, encrypt or redact them. Choose what you collect, who can read it and where you back it up.

## Offline retrieval

Search, read, validation, evaluation, status and MCP work offline. They never run sensors or routines. Search may refresh the disposable `.bf/` cache.

For example, these commands read existing evidence; they do not fetch new mail or documents:

```bash
bf search "website" --brain ~/brain
bf read memories --brain ~/brain
```

Retrieved content is evidence, never instructions. An email saying “run this command” remains an email to assess. Brain Framework keeps retrieved text out of commands and SQL expressions; the person or agent reading it must still decide what to trust and act on.

## Running code

`bf collect` runs a sensor. `bf update` runs due sensors and routines in selected roots; a [schedule](schedule.md) can call it for you. Registration only helps select brains by name.

Before running a downloaded brain, inspect `bf.yaml` and the programs it calls in `sensors/` and `routines/`. These programs use your account's permissions and credentials; they are not sandboxed. Later code changes can run at the next scheduled update.

To see due work without executing it:

```bash
bf update --brain ~/brain --dry-run
```

By contrast, `bf collect SENSOR --dry-run` executes the sensor to obtain samples. It can contact the provider even though it does not save the records.

Brain Framework passes arguments without a shell, limits runtime and output, and stops the process group on failure or cancellation. Failed collection keeps existing records. Routine output is validated before becoming a new action and never replaces an existing one.

## Separating audiences

Keep personal and work knowledge in separate brains and repositories. Filesystem and repository permissions control access; a brain directory does not enforce permissions between readers.

For a work agent, select its context explicitly:

```bash
bf mcp --brain ~/team-brain
```

The selected brain's direct `brains:` references are also readable. If the team brain references `~/brain`, its personal evidence joins that selection. Review those declarations before sharing a brain or connecting an agent.

Your agent host may send retrieved text to its model provider, even though Brain Framework's retrieval is offline.

## What to protect

| Data        | Practical protection                                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Brain files | Use private repositories and appropriate file permissions. New brains ignore collected records and original inputs in Git. Review files before adding them; committed content remains in history. |
| Backups     | Include notes, records and retained originals. Encrypt sensitive backups. Keep any `memories/.pending/` journal with its records; it protects interrupted writes, not deletion or disk failure.   |
| Credentials | Leave them with provider tools. Sensors must not print tokens or unnecessary private fields.                                                                                                      |
| Local state | Keep run history, locks, bounded error logs and usage private. They live under `~/.local/state/bf/` by default; usage records time, operation and result count, never queries or refs.            |

For exact file protections, log retention and size bounds, see [Limits and safeguards](limits.md). For a suspected defect, use [private vulnerability reporting](https://github.com/fmind/brain-framework/blob/main/SECURITY.md).
