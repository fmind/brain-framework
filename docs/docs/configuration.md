# Configuration and brain selection

Most commands need only a brain directory. After [Getting started](getting-started.md), this works from anywhere:

```bash
bf read projects/new-website.md#decision --brain ~/brain
```

The reply contains the saved reason for choosing a single product page. You do not need a global configuration file or registration.

Each brain's `bf.yaml` holds its name, optional references, [schema](schema.md), [sensors](sensors.md) and [routines](routines.md). Unknown settings, duplicate keys and invalid values fail visibly. The [JSON Schema](../bf.schema.json) describes the current format.

The walkthrough uses `~/brain`; any directory can hold a brain. Its path and configured name are separate: `bf init ~/brains/default --name brain` creates a folder at `~/brains/default` whose links start with `bf://brain/`. See [location choices](getting-started.md#choose-a-location).

## Select a brain

Commands use the first selection supplied in this order; an invalid explicit choice fails instead of falling through:

1. `--brain NAME|PATH`.
1. The `BF_BRAIN` environment variable.
1. The brain containing the working directory.
1. All brains in the optional machine registry.

Use a path when automating a particular brain. A name resolves through the enclosing brain, its direct references or the registry. A registered name takes precedence over a same-named directory.

For example, this selects the team brain even if you run it inside `~/brain` or have set `BF_BRAIN` to another directory:

```bash
bf read projects --brain ~/team-brain
```

Search, read and evaluation include selected roots and their direct references. Maintenance commands, including `update`, act only on selected roots. From outside a brain, an unqualified `bf update` can therefore run due programs in every registered brain.

## Related brains

To search a sibling team brain from your personal brain, replace `brains: {}` in `~/brain/bf.yaml` with this entry. If `brains:` already lists references, add `team-brain` beneath it. Keep the other settings and avoid duplicate keys:

```yaml
# https://fmind.github.io/brain-framework/docs/configuration/
brains:
  team-brain:
    path: ../team-brain
```

The key must match the destination's `bf.yaml` name. Relative paths resolve from the declaring brain; absolute paths and `~` also work. Up to 32 direct references are allowed. References never recurse, download repositories or run programs.

```bash
bf read projects --brain ~/brain
bf update --dry-run --brain ~/brain
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

Paths must be absolute or start with `~`. Names are unique; registering a second directory under the same name fails. Registration writes atomically, uses owner-only file permissions and preserves leading comments.

Automatic selection skips registered directories absent from this machine. Explicit selection fails if its brain is missing. To remove a default selection, delete its registry entry; the brain files stay in place.

## Local state

`XDG_CONFIG_HOME` overrides `~/.config`; `XDG_STATE_HOME` overrides `~/.local/state`. Run history, locks, usage and logs stay under the state directory in `bf/`. Processes accessing one physical brain must share this directory.

State is machine-local. Removing it loses collection windows and freshness history, so the next run starts from `lookback`. It does not delete notes or records. See [scheduling](schedule.md) and [file safeguards](limits.md#files).
