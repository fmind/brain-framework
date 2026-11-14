---
icon: lucide/settings
description: Configure a brain, choose which brain a command uses, register brains and locate local state.
---

# Configuration and brain selection

Commands inside a brain folder find `bf.yaml` automatically with zero global setup:

```bash
cd ~/brain
bf read projects/new-website.md#decision
```

`bf.yaml` declares format `version: 10`, `name`, and optional [`fields`](schema.md#shared-fields-and-sensor-mappings), [`defaults`](prompts.md#assistant-settings), [`jobs`](prompts.md), [`watch`](schedule.md#watch-preferences) and [`validate`](checks.md#validate) sections ([JSON Schema](../bf.schema.json)). Unknown keys, invalid values and duplicate YAML keys fail visibly naming the file and location.

| File                       | Purpose                                            | Required                                |
| -------------------------- | -------------------------------------------------- | --------------------------------------- |
| `bf.yaml`                  | Brain name, ontology, programs and watch settings. | Always (`bf init` creates it).          |
| `evals/*.yaml`             | Retrieval regression suites.                       | Optional.                               |
| `~/.config/bf/config.yaml` | Machine registry mapping brain names to paths.     | Optional (for `--brain NAME` anywhere). |

## Select a brain

Every command acts on **one** selected brain, resolved in strict precedence order (an invalid explicit choice fails immediately rather than falling back):

| Precedence              | Retrieval, checks, `status`                  | Execution (`collect`, `run`, `post`, `wake`, `queue`, `update`, `watch`) |
| ----------------------- | -------------------------------------------- | ------------------------------------------------------------------------ |
| 1. `--brain NAME\|PATH` | Named or path brain.                         | Named (registered or enclosing) or path brain.                           |
| 2. `BF_BRAIN`           | Named or path brain.                         | Same single-brain rule.                                                  |
| 3. Enclosing directory  | Nearest parent holding user-owned `bf.yaml`. | Same.                                                                    |
| 4. Registry `default`   | Registered `default` brain, else fail.       | Never uses `default`: pass `--brain` or run inside the brain.            |

- `--brain ""` exits 2; selecting a subfolder without `bf.yaml` fails naming the root requirement; enclosing directories owned by another OS user are refused (`is not a regular file owned by you`).
- Execution commands (`collect`, `run`, `post`, `wake`, `queue`, `update`, `watch`) never fall back to the registry `default`.

## Optional machine registration {#related-brains}

Register brains in `~/.config/bf/config.yaml` (or `$XDG_CONFIG_HOME/bf/config.yaml`, regular file <= 1 MiB, schema `bf schema --kind registry`) to query them by name from any directory:

```bash
bf register ~/brain --default
bf search "visitors clear explanation" --brain brain
bf unregister brain
```

- `bf register` updates an existing name automatically when its old path moved or reached the same physical directory (`replaced` in reply), and refuses when another reachable brain already claims that name. Registration never runs programs.
- `--default` sets `default:` in `~/.config/bf/config.yaml` (used by retrieval/read/status/validate/build/eval outside a brain directory). `bf unregister` removes an entry and clears `default` if it named that brain.

To rename a brain: drain `bf queue`, update `name:` in `bf.yaml` and `bf://` links, update `~/.config/bf/config.yaml` (`bf register`), and run `bf queue` to rebind the drained queue.

## Local state

| Location                          | Contents                                               | Disposable |
| --------------------------------- | ------------------------------------------------------ | ---------- |
| `<brain>/.bf/cache.sqlite3`       | SQLite search and graph cache                          | Yes        |
| `<brain>/.bf/locks/` and `state/` | Brain locks and per-program run history                | —          |
| `<brain>/.bf/runtime.sqlite3`     | Durable [runtime queue](#assistant-queue-state)        | **No**     |
| `<brain>/.bf/usage.sqlite3`       | Aggregate usage counts (no queries, refs or paths)     | Yes        |
| `<brain>/logs/`                   | Bounded job stderr logs (gitignored)                   | Yes        |
| `$XDG_CONFIG_HOME/bf/config.yaml` | Optional machine registry (`~/.config/bf/config.yaml`) | —          |

`<brain>/.bf/` is mode `700`, owned by your OS user, gitignored, and rejects symlinks. Moving a brain whose queue still holds unresolved work from its previous path or name refuses job execution until `bf queue` is drained ([move or rename a brain](channels.md#move-or-rename-a-brain)).

### Runtime queue state {#assistant-queue-state}

`<brain>/.bf/runtime.sqlite3` holds durable job state, deliveries, events and wakes — back it up with your brain and never delete it with disposable `.bf/cache.sqlite3` ([where the queue lives](channels.md#where-the-queue-lives)).
