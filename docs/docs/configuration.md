---
description: Choose brains and configure references, the optional machine registry and local state.
---

# Configuration and brain selection

Most commands discover the brain from your working directory:

```bash
cd ~/brain
bf read projects/new-website.md#decision
```

The reply contains the saved reason for choosing a single product page. You do not need a global configuration file or registration.

Each brain's `bf.yaml` holds its name, optional references, [schema](schema.md), [sensors](sensors.md), [routines](routines.md) and optional [watch preferences](schedule.md#watch-preferences). Unknown settings, duplicate keys and invalid values fail visibly. The [JSON Schema](../bf.schema.json) describes the current format; [editor schemas](schema.md#editor-schemas) also cover the machine registry and evaluation suites.

The walkthrough uses `~/brain`; any directory can hold a brain. Its path and configured name are separate: `bf init ~/brains/default --name brain` creates a folder at `~/brains/default` whose links start with `bf://brain/`. See [location choices](getting-started.md#choose-a-location).

## Configuration files

| File                       | Purpose                                                 | Needed when…                                         |
| -------------------------- | ------------------------------------------------------- | ---------------------------------------------------- |
| `bf.yaml`                  | Brain identity, fields, programs and watch preferences. | Always; `bf init` creates it.                        |
| `~/.config/bf/config.yaml` | Optional machine registry of names and paths.           | You select brains by name outside their directories. |
| `evals/*.yaml`             | Questions and expected evidence.                        | You want repeatable retrieval checks.                |

See [editor schemas](schema.md#editor-schemas) for validation. Relative brain references start at `bf.yaml`; execution commands run from the brain root.

## Select a brain

Commands use the first selection supplied in this order; an invalid explicit choice fails instead of falling through:

1. `--brain NAME|PATH`.
1. The `BF_BRAIN` environment variable.
1. The brain containing the working directory.
1. Retrieval only: all brains in the optional machine registry.

Use a path when automating a particular brain. A name resolves through your machine registry first, then through the enclosing brain and its direct references, then as a directory below the working directory. If the enclosing brain or one of its references claims a registered name for a different directory, selection fails with an `ambiguous brain name` error; pass `--brain PATH` instead. A checkout you are working in therefore cannot replace your registered brain. An enclosing `bf.yaml` that fails to load claims no name, so it does not block a registered one; a same-named reference that fails to load stops selection with an error naming its `brains.NAME` entry.

The enclosing brain is the nearest directory above the working directory that holds a `bf.yaml`. Like Git, BF trusts it only when you own both the directory and its `bf.yaml`, a regular file. Otherwise every command that needs it fails with `bf.yaml is not a regular file owned by you`; pass `--brain PATH` to select a brain deliberately. Such a brain claims no name either: below it, `--brain NAME` and `BF_BRAIN=NAME` still select your registered brain.

For example, this selects the team brain even if you run it inside `~/brain` or have set `BF_BRAIN` to another directory:

```bash
bf read projects --brain ~/team-brain
```

Search, read and evaluation include selected roots and their direct references. `build`, `eval`, `validate` and `status --watch` need one root: when several registered brains are present on this machine and none is selected, they ask for `--brain NAME`. Registered brains absent here are ignored by these commands; search, read and `bf status --check` report them. When every registered brain is absent, commands name them and ask you to restore them or remove their registry entries. `update`, `collect`, `watch` and `schedule` run programs in exactly one brain: `--brain`, `BF_BRAIN` or the enclosing brain. They never fall back to all registered brains: registration selects brains for retrieval only, although a `--brain` or `BF_BRAIN` name still resolves through the registry. For these commands, a name must be registered or be the enclosing brain's own; a referenced brain or a directory below the working directory fails with `neither registered nor the enclosing brain`, so review its programs and select it by path. Outside a brain, an unqualified `bf update` fails with `pass --brain PATH or run inside the brain`.

## Related brains

To search a sibling team brain from your personal brain, add this `brains:` entry to `~/brain/bf.yaml`. A new brain has no `brains:` key; if yours already has one, add only `team-brain` beneath it. Keep the other settings:

```yaml
# https://fmind.github.io/brain-framework/docs/configuration/
brains:
  team-brain:
    path: ../team-brain
```

The key must match the destination's `bf.yaml` name. Relative paths resolve from the declaring brain; absolute paths and `~` also work. Up to 32 direct references are allowed. References never recurse, download repositories or run programs.

```bash
bf read projects
bf update --dry-run
```

The first command lists projects from both brains. The second previews only your personal brain's due programs; it executes nothing.

Missing or invalid references appear under `problems`; healthy brains still answer. Directories claiming the same brain name are excluded, and repeated physical directories are searched once. An incomplete empty reply does not establish absence.

Brain names start with a lowercase letter and contain lowercase letters, digits or hyphens, up to 64 characters. Keep the name stable across clones: it is the namespace in `bf://team-brain/...` addresses. If you rename a brain, update its BF links, incoming declarations and registry key explicitly.

## Optional machine registration

Register a brain when you want to select it by name outside its directory:

```bash
bf register ~/brain
bf search "visitors clear explanation" --brain brain
```

The search returns the same New website decision as selection by path. The registry at `~/.config/bf/config.yaml` stores names and paths; with both brains registered, it looks like this:

```yaml
# https://fmind.github.io/brain-framework/
brains:
  brain:
    path: /home/me/brain
  team-brain:
    path: /home/me/team-brain
```

Paths must be absolute or start with `~`, without control characters. Names are unique; registering a second directory under the same name fails. Registration writes atomically, uses owner-only file permissions and preserves leading comments. `bf schema --kind registry` prints the installed registry schema without opening the registry; path resolution remains a runtime check.

Registration never runs a brain's programs. Before running a cloned team brain's sensors or routines yourself, review its `bf.yaml`, `sensors/` and `routines/`, then select it explicitly: `bf update --brain ~/team-brain`.

The registry must be a regular file of at most 1 MiB. Symlinks and special files are rejected; replace them with a regular configuration file. A dotfiles manager that links the file itself, rather than `~/.config/bf/`, needs a copy instead, or a link of the parent directory. Commands selecting an explicit brain path or the enclosing brain still work without a valid registry; selecting by name fails until you repair it.

Outside a brain, search and read still answer from the registered brains present on this machine, but report each absent one under `problems` as `registered brain directory is absent on this machine`; `bf status` lists it with that error and `bf status --check` fails. Explicit selection fails if its brain is missing. To remove a default selection, delete its registry entry; the brain files stay in place.

## Local state

`XDG_CONFIG_HOME` overrides `~/.config`; `XDG_STATE_HOME` overrides `~/.local/state`. As the XDG specification requires, empty and relative values are ignored. Run history, locks, usage and logs stay under the state directory in `bf/`, which must be owned by you. BF creates `bf/` and its directories with mode 700 and restricts an existing `bf/` to it; a directory below it that is owned by another account or open to other users fails with `state directory must be owned by you with mode 700`. Processes accessing one physical brain must share this directory.

Locks follow the physical brain directory: two paths to it, such as a bind mount, share the writer lock and each program's lock. Run history, usage and logs follow the brain's resolved path instead. After moving or renaming a brain directory, the next run starts a new history: sources show `never` and window sensors restart from `lookback`. Update the `path` of its registry entry, regenerate scheduler files and backfill a gap with `bf collect SENSOR --since DATE`.

State is machine-local. Removing it loses collection windows and freshness history, so the next run starts from `lookback`. It does not delete notes or records. See [scheduling](schedule.md) and [file safeguards](limits.md#files).
