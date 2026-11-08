---
icon: lucide/package
description: Install or update Brain Framework, match documentation to your version and verify the result.
---

# Install and update

## Install

Brain Framework supports Linux and macOS and requires Python 3.14 or newer. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then let it manage BF's isolated environment:

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

If your shell cannot find `bf`, run `uv tool update-shell` and open a new shell. Then follow [Getting started](getting-started.md). To serve an agent host over [MCP](mcp.md), install `'brain-framework[mcp]'` instead; upgrades keep the extra.

`--python 3.14` selects the tested Python, which uv downloads if needed and remembers for later upgrades. Without it, uv may pick a newer Python for which some dependencies publish no prebuilt packages yet.

<details markdown="1">
<summary>If a command asks you to reinstall BF</summary>

Three installation problems stop commands with a message naming the fix:

- **PyYAML without libyaml.** BF reads YAML only through libyaml. When PyYAML was built without it, as from source on a newer Python, every command reading `bf.yaml` stops. Fix: `uv tool install --reinstall --python 3.14 brain-framework`.
- **SQLite too old.** The search cache needs SQLite 3.35.0 or newer with FTS5 and JSON functions, which uv's managed Pythons include. When a system Python lacks them, search, read, status and build stop. Fix: `uv tool install --reinstall --managed-python --python 3.14 brain-framework`.

- **Missing `mcp` extra.** `bf mcp needs the mcp extra`. Fix: `uv tool install --python 3.14 'brain-framework[mcp]'`, then restart the agent host.

With MCP, reinstall `'brain-framework[mcp]'` in the first two fixes too: a reinstall without the extra drops it.

</details>

## Match the docs to your version

This site follows the repository's current code, which can be newer than your installation. `bf --version` names your runtime; `bf COMMAND --help` lists its options. Copy example sensors, routines and demos from the release tag matching `bf --version`, as the guides' commands do. The brain format, declared as `version:` in `bf.yaml` and each `evals/*.yaml`, is separate from the package version; [versioning and stability](#versioning-and-stability) says what it covers.

## Versioning and stability

A release number reads MAJOR.MINOR.PATCH. Within one major release:

- `search` and `read` [replies](retrieval.md#reply-schemas) only gain optional fields.
- `bf.yaml`, evaluation suites and the machine registry only widen: a file that loads keeps loading.
- The brain format `version` stays the same.

Minor releases add features; patch releases fix bugs, including input BF misread. A major release ships whenever the design needs it and batches related breaks where practical. It adds no compatibility layer; its manual upgrade steps are in the [changelog](../changelog.md).

A new brain format `version` means stored files need converting: record files, the `memories/` layout, notes, evaluation suites or the fields `bf.yaml` declares. Other breaks keep `version`, with manual upgrade steps: changed program settings, such as sensors, routines, assistants, channels and watch, or a stricter rule that rejects input which loaded before. [Contributing](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md#keep-the-contract) holds the full rule.

## Update

Before upgrading:

1. Stop watchers and scheduled collection.
1. Read the [changelog](../changelog.md). Run the steps that need the previous release first, such as draining its assistant queue.
1. Back up the idle brain and its durable assistant queue. The queue lives outside the brain, under `$XDG_STATE_HOME/bf/runtime/` (normally `~/.local/state/bf/runtime/`). A copy of its `queue.sqlite3` taken while no BF process runs is complete; see [queue recovery](channels.md#queue-and-recovery).

Then upgrade and confirm the new version:

```bash
uv tool upgrade brain-framework
bf --version
```

Apply the upgrade steps of each release newer than your previous version, oldest first. Finish a major release's steps before collecting again.

- **Assistant or channel changes:** resolve the work `bf queue` lists first. A queued job keeps the settings it was queued with and fails once they change.
- **Updated examples:** when a release asks you to copy updated example sensors, routines or hooks, copy them from the tag matching `bf --version` and keep your adaptations.

Then check the brain and update the agent skills:

```bash
cd ~/brain
bf validate
bf eval
bf status --check
bf skills ~/.agents/skills
```

Expect `"valid":true` from validation, `"passed":true` from your retrieval cases and exit 0 from the status check. Otherwise fix the reported problem first, with [Troubleshooting](troubleshooting.md).

`bf skills` updates the skills it installed in that folder. Repeat it for each folder you installed them into, such as `~/.claude/skills` for Claude Code. It leaves a folder unchanged and exits 1 when the folder reports:

- `modified`: you edited it.
- `unmanaged`: it has no BF manifest.
- `newer`: a newer `bf` installed a different copy.

See [skill installation](agents.md#install-the-skills) before passing `--force`.

`bf init` writes a brain's files once, so a release's new defaults never reach an existing brain by themselves:

- Merge new `AGENTS.md` guidance by hand, as [Refresh brain instructions](agents.md#refresh-brain-instructions) shows.
- Add new [standard vocabulary](schema.md#standard-vocabulary) fields, such as `part-of` and `label`, to use the sensors that print them.
- Add or update the [editor schema](schema.md#editor-schemas) line of `bf.yaml` and `evals/*.yaml`, so its version matches `bf --version`.

Edit a [watch service](schedule.md#run-watch-as-a-service) or timer when the changelog asks or the `bf` executable moved.

Use the same release on every clone and collecting machine of a team brain. Restart agent hosts and watchers after updating, then check collection health with `bf status --check` on the collecting machine.

## Pin a brain's runtime

A brain can pin its own BF release as a uv project, so every clone runs the same version. This pins the version your installed `bf` reports:

```bash
cd ~/brain
uv init --bare --python 3.14
uv add "brain-framework==$(bf --version)"
echo /.venv/ >> .gitignore
uv run --locked bf --version
```

Commit `pyproject.toml` and `uv.lock`. Run commands that execute programs, such as `bf update`, through the pin:

- Inside the brain: `uv run --locked bf update`.
- Elsewhere: `uv run --project ~/brain --locked bf update --brain ~/brain`.

Search, read and checks need no pin. The agent skills run them with your installed `bf` in every brain, and stop to ask when a pin names another major version. Upgrade your installed `bf` and the pin together; upgrade the pin with `uv add "brain-framework==X.Y.Z"`.

Run a [watch service](schedule.md#run-watch-as-a-service) through the pin too, with absolute paths and the folder of `uv` in its `PATH`:

```bash
uv run --project ~/brain --locked bf watch --json --brain ~/brain
```

`uv run` puts the pinned `bf` first on the `PATH` of the programs it starts, so routines that call `bf` use the same release. Write Git hooks the same way, such as `exec uv run --locked bf run --hook pre-commit` or `exec uv run --locked bf run --hook pre-push -- "$@"`.

Run a pin only in a brain you created or reviewed: it runs with your permissions. `uv run` trusts the brain's files:

- It installs and runs whatever its `uv.lock` names.
- It uses the interpreter its `.python-version` names and the settings of its `uv.toml`.
- It runs an existing `.venv/` as is.

Before running or upgrading a downloaded or shared brain's pin:

- Review those files and `pyproject.toml`, like `sensors/` and `routines/`.
- Delete a `.venv/` it ships before the first pinned run.
- Never run the pin of a brain whose Git tracks a `.venv/` (`git ls-files .venv` prints files): every checkout restores it.

Search or read a downloaded or shared brain with your installed `bf` ([privacy](privacy.md#running-code)).
