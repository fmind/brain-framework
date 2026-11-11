---
icon: lucide/settings
description: Configure a brain, choose which brains a command uses, declare related brains and locate local state.
---

# Configuration and brain selection

Commands inside a brain folder find `bf.yaml` automatically with zero global setup:

```bash
cd ~/brain
bf read projects/new-website.md#decision
```

`bf.yaml` declares format `version: 8`, `name`, and optional `brains`, [`fields`](schema.md#shared-fields-and-sensor-mappings), [`sensors`](sensors.md#sensor-settings), [`routines`](routines.md#routine-settings), [`assistants`](prompts.md#assistant-settings), [`channels`](channels.md), [`watch`](schedule.md#watch-preferences) and [`validate`](checks.md#validate) sections ([JSON Schema](../bf.schema.json)). Unknown keys, invalid values and duplicate YAML keys fail visibly naming the file and location.

| File                       | Purpose                                            | Required                                |
| -------------------------- | -------------------------------------------------- | --------------------------------------- |
| `bf.yaml`                  | Brain name, ontology, programs and watch settings. | Always (`bf init` creates it).          |
| `evals/*.yaml`             | Retrieval regression suites.                       | Optional.                               |
| `~/.config/bf/config.yaml` | Machine registry mapping brain names to paths.     | Optional (for `--brain NAME` anywhere). |

## Select a brain

Commands resolve brain selection in strict precedence order (an invalid explicit choice fails immediately rather than falling back):

| Precedence              | `search`, `read`, `export`, `mcp`, `status`  | `validate`, `eval`, `build`, `forget`, `action`, `status --watch` | `collect`, `run`, `channel`, `queue`, `update`, `watch` |
| ----------------------- | -------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------- |
| 1. `--brain NAME\|PATH` | Named or path brain.                         | Named or path brain.                                              | Named (registered or enclosing) or path brain.          |
| 2. `BF_BRAIN`           | Named or path brain.                         | Named or path brain.                                              | Same single-brain rule.                                 |
| 3. Enclosing directory  | Nearest parent holding user-owned `bf.yaml`. | Same.                                                             | Same.                                                   |
| 4. Fallback             | Every registered brain on this machine.      | Sole registered brain, else ask for `--brain`.                    | Fail: `pass --brain PATH or run inside the brain`.      |

Scope rules:

- **Retrieval (`search`, `read`, `export`, `mcp`, `eval`)**: covers the selected brains plus their direct `brains:` references (never recursively).
- **Checks and cache (`validate`, `build`)**: act on the selected brain alone (`validate` reports cross-brain links under `unresolved`).
- **Execution and mutations (`collect`, `run`, `action`, `channel`, `queue`, `update`, `watch`, `forget`)**: act on one brain only, never its `brains:` references.
- **Safety checks**: `--brain ""` exits 2; selecting a subfolder without `bf.yaml` fails naming the root requirement; enclosing directories owned by another OS user are refused (`is not a regular file owned by you`).

## Related brains

Include a team or reference brain in personal searches by declaring it under `brains:` in `~/brain/bf.yaml` (up to 32 direct references, relative to the declaring brain, `~` or absolute):

```yaml
# https://fmind.github.io/brain-framework/docs/configuration/
brains:
  team-brain:
    path: ../team-brain
```

The key must match the target brain's `name` (`[a-z][a-z0-9-]{0,63}`, stable across clones because it forms `bf://NAME/...` addresses). Unavailable references report under `problems` while healthy brains keep answering.

To rename a brain: drain `bf queue`, update `name:` in `bf.yaml` and `bf://` links, update `~/.config/bf/config.yaml` (`bf register`), and run `bf queue` to rebind the drained queue.

## Optional machine registration

Register brains in `~/.config/bf/config.yaml` (or `$XDG_CONFIG_HOME/bf/config.yaml`, regular file <= 1 MiB, schema `bf schema --kind registry`) to query them by name from any directory:

```bash
bf register ~/brain
bf search "visitors clear explanation" --brain brain
```

`bf register` updates an existing name automatically when its old path moved or reached the same physical directory (`replaced` in reply), and refuses when another reachable brain already claims that name. Registration never runs programs.

## Local state

| Location                          | Contents                                           | Default                      |
| --------------------------------- | -------------------------------------------------- | ---------------------------- |
| `$XDG_STATE_HOME/bf/`             | Locks, run history, program state, `usage.jsonl`   | `~/.local/state/bf/`         |
| `$XDG_STATE_HOME/bf/runtime/`     | Durable [assistant queues](#assistant-queue-state) | `~/.local/state/bf/runtime/` |
| `$XDG_CONFIG_HOME/bf/config.yaml` | Optional machine registry                          | `~/.config/bf/config.yaml`   |
| `<brain>/logs/`                   | Bounded program stderr logs (gitignored)           | —                            |

State directory invariants:

- Must sit outside the brain, owned by your OS user (mode `700`/`600`), with no symlinks at or below `$XDG_STATE_HOME/bf` (`state directory may not contain symlinks`).
- Writers lock both the state lock and the physical brain directory (keep a single shared `XDG_STATE_HOME` on filesystems such as NFS that lack directory locks).
- Moving a brain resets path-keyed run history (`never` until next run); drain `bf queue` before moving, run `bf register` at the new path, run `bf queue` to rebind, and update service files.

### Assistant queue state

`$XDG_STATE_HOME/bf/runtime/<dev>-<ino>/queue.sqlite3` holds durable assistant jobs, channel deliveries and per-slot wakes — back it up with your brain and never delete it with disposable `.bf/` caches ([where the queue lives](channels.md#where-the-queue-lives), [move or rename a brain](channels.md#move-or-rename-a-brain)).
