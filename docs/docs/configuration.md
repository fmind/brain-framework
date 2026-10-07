---
icon: lucide/settings
description: Configure a brain, choose which brains a command uses, declare related brains and locate local state.
---

# Configuration and brain selection

Most commands find the brain from your working directory; no global configuration or registration is needed:

```bash
cd ~/brain
bf read projects/new-website.md#decision
```

Each brain's `bf.yaml` holds its format `version`, its `name` and optional `brains`, [`fields`](schema.md#shared-fields-and-sensor-mappings), [`sensors`](sensors.md#sensor-settings), [`routines`](routines.md#routine-settings) and [`watch`](schedule.md#watch-preferences) sections. Unknown keys, duplicate keys and invalid values fail with the file and field named. An optional section left empty, such as `sensors:` with every entry commented out, holds nothing, and an empty `watch:` keeps its defaults. The [JSON Schema](../bf.schema.json) describes the format; see [editor schemas](schema.md#editor-schemas).

| File                       | Purpose                                             | Needed when…                                     |
| -------------------------- | --------------------------------------------------- | ------------------------------------------------ |
| `bf.yaml`                  | Brain name, fields, programs and watch preferences. | Always; `bf init` creates it.                    |
| `evals/*.yaml`             | Questions and expected evidence.                    | You want repeatable retrieval checks.            |
| `~/.config/bf/config.yaml` | Optional machine registry of brain names and paths. | You select brains by name outside their folders. |

## Select a brain

Commands take the first selection available, in this order. An invalid explicit choice fails instead of falling through:

| Selection                   | `search`, `read`, `export`, `mcp`, `status` | `validate`, `eval`, `build`, `status --watch`            | `collect`, `run`, `update`, `watch`, `schedule`                 |
| --------------------------- | ------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------- |
| 1. `--brain NAME` or `PATH` | That brain.                                 | That brain.                                              | That brain; a name must be registered or the enclosing brain's. |
| 2. `BF_BRAIN`               | That brain.                                 | That brain.                                              | That brain, under the same rule.                                |
| 3. The enclosing brain      | The nearest folder above with a `bf.yaml`.  | The same.                                                | The same.                                                       |
| 4. Otherwise                | Every registered brain present here.        | The only registered brain present, or ask for `--brain`. | Fail: `pass --brain PATH or run inside the brain`.              |

Retrieval also includes each selected brain's direct `brains:` references: `search`, `read`, `export`, `mcp` and the cases of `eval`. `status --check` fails while one is unavailable. `validate` and `build` act on the selected brain only; validation lists targets in other brains under [`unresolved`](link-reference.md#across-brains). Execution never includes references: programs run in exactly one brain.

- **Names** resolve through the machine registry first, then the enclosing brain and its references, then a folder below the working directory. When the enclosing brain or a reference claims a registered name for another folder, selection fails as `ambiguous brain name`: pass a path. A name that matches nothing suggests close known names, such as `did you mean work?` for `--brain wrok`.
- **A selected brain** is a folder holding `bf.yaml`. Selecting a subfolder such as `projects/`, or a parent, fails before anything runs, naming the selection as given: `PATH has no bf.yaml; pass the brain's root directory, or create a brain with bf init`, or `registered brain NAME has no bf.yaml`.
- **An empty `--brain`** is invalid input (exit 2), never a request for the default; omit the option instead. An empty `BF_BRAIN` counts as unset.
- **The enclosing brain** counts only when you own its folder and its `bf.yaml` is a regular file, as Git requires. Otherwise commands fail naming it from the working directory, such as `../bf.yaml is not a regular file owned by you`.
- **Absent registered brains** are reported under `problems` by search and read, and fail `bf status --check`. Selecting one by name fails: restore it, run `bf register` at its new place, or remove its entry. A registered folder that exists but cannot be reached, such as a locked folder, is reported the same way as `registered brain directory exists but cannot be reached; restore access to it`.

For example, this selects the team brain from anywhere, even inside `~/brain`:

```bash
bf read projects --brain ~/team-brain
```

## Related brains

To search a sibling team brain from your personal brain, declare it in `~/brain/bf.yaml`. If a `brains:` key exists, add only the entry:

```yaml
# https://fmind.github.io/brain-framework/docs/configuration/
brains:
  team-brain:
    path: ../team-brain
```

The key must match the target's `name`. Relative paths start from the declaring brain; absolute and `~` paths work too. Up to 32 direct references are allowed; they never recurse, download anything or run programs.

```bash
bf read projects
bf update --dry-run
```

The first command lists projects from both brains; the second previews only your personal brain's due programs. A missing or invalid reference appears under `problems` while the other brains still answer. Folders claiming the same name are excluded, and one physical folder is searched once.

Brain names start with a lowercase letter and hold lowercase letters, digits or hyphens, up to 64 characters. Without `--name`, `bf init` derives the name from the folder, lowercased with accents folded and other characters as hyphens (`Équipe produit` becomes `equipe-produit`); a folder name that cannot form one, such as `Øre`, needs `--name`. Keep the name stable across clones: it is the namespace of `bf://team-brain/...` addresses. After renaming a brain, update its links and references, remove its old registry entry, then run `bf register`.

## Optional machine registration

Register a brain to select it by name outside its folder:

```bash
bf register ~/brain
bf search "visitors clear explanation" --brain brain
```

The search returns the same decision. The registry, `~/.config/bf/config.yaml`, maps names to paths:

```yaml
# https://fmind.github.io/brain-framework/docs/configuration/
brains:
  brain:
    path: /home/me/brain
  team-brain:
    path: /home/me/team-brain
```

Paths are absolute or start with `~`. Writes are atomic, owner-only and keep leading comments. The registry must be a regular file of at most 1 MiB; a dotfiles manager should link its parent folder, not the file. `bf schema --kind registry` prints its JSON Schema.

Names are unique. After moving a brain, run `bf register` at its new place: it takes over the name when the registered path no longer exists, as after a move or while a network share is not mounted, or reaches the same brain through another path. The reply names the old path:

```json
{
  "brain": "brain",
  "config": "/home/me/.config/bf/config.yaml",
  "path": "/home/me/brains/brain",
  "replaced": "/home/me/brain"
}
```

Any other conflict fails and names the entry to remove from the registry, such as another brain registered under that name, or a registered folder that exists but cannot be reached, like a locked folder or a disconnected network mount: `cannot reach the brain registered as NAME at PATH`.

Registration never runs a brain's programs. Before running a cloned brain's sensors or routines, review its `bf.yaml`, `sensors/` and `routines/`, then select it explicitly, as with `bf update --brain ~/team-brain`. Delete a registry entry to stop selecting a brain by default; its files stay in place, and an empty `brains:` is valid.

## Local state

Run history, locks and usage counts stay on this machine, in the `bf/` folder of the state folder: `XDG_STATE_HOME`, by default `~/.local/state`. Writers also lock the brain folder itself, so processes with different state folders, or accounts sharing a brain, never write at once; a network filesystem that cannot lock a folder, such as NFS, relies on the state lock alone, so keep one `XDG_STATE_HOME` for every process there. `XDG_CONFIG_HOME` (default `~/.config`) holds the registry. Empty and relative values are ignored. Program logs live in the brain's own `logs/` folder instead, ignored by Git.

The state folder must be outside the brain. BF creates `bf/` with mode 700 and fails when another account owns it; set `XDG_STATE_HOME` to a writable folder if needed. The state folder, its `bf/` folder and everything below must be real folders: a symbolic link there makes every command fail with `state directory may not contain symlinks`. Links above them, such as `/home` pointing to `/var/home`, are fine.

Processes using one physical brain share its locks, even through another path such as a bind mount, so they must share the state folder. Run history and usage follow the brain's path instead. After moving a brain, sources show `never` and window sensors restart from `lookback`: update its registry entry with `bf register`, regenerate schedules and backfill any gap with `bf collect SENSOR --since DATE`. Removing state loses collection windows and freshness history, never notes or records.
