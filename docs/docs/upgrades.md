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

`--python 3.14` selects the tested Python, which uv downloads if needed. Without it, uv may pick a newer Python for which some dependencies publish no prebuilt packages yet. BF reads YAML only through libyaml: when PyYAML was built without it, as from source on such a Python, every command reading `bf.yaml` stops with a message naming the fix, `uv tool install --reinstall --python 3.14 brain-framework` (`'brain-framework[mcp]'` with MCP, since a reinstall without the extra drops it). uv remembers the choice for later upgrades. If your shell cannot find `bf`, run `uv tool update-shell` and open a new shell. Then follow [Getting started](getting-started.md). To serve an agent host over [MCP](mcp.md), install `'brain-framework[mcp]'` instead; upgrades keep the extra.

The search cache needs SQLite 3.35.0 or newer with FTS5 and JSON functions, which uv's managed Pythons include. When a system Python lacks them, search, read, status and build stop with a message naming the fix: `uv tool install --reinstall --managed-python --python 3.14 brain-framework`.

## Match the docs to your version

This site follows the repository's current code, which can be newer than your installation. `bf --version` names your runtime, and `bf COMMAND --help` lists its options. Copy example sensors, routines and demos from the release tag matching `bf --version`, as the guides' commands do. The brain format, declared as `version:` in `bf.yaml` and each `evals/*.yaml`, is separate from the package version.

## Update

Stop watchers and scheduled collection, read the [changelog](../changelog.md) and back up the idle brain, together with its durable assistant queue under `$XDG_STATE_HOME/bf/runtime/` (normally `~/.local/state/bf/runtime/`), which lives outside the brain: a copy of its `queue.sqlite3` taken while no BF process runs is complete; see [queue recovery](channels.md#queue-and-recovery). Then upgrade and confirm the new version:

```bash
uv tool upgrade brain-framework
bf --version
```

Apply the upgrade steps of each release newer than your previous version, oldest first, and finish a major release's steps before collecting again. Resolve the work `bf queue` lists before a step that changes assistant or channel configuration: a queued job keeps the settings it was queued with and fails once they change. When a release asks you to copy updated example sensors, routines or hooks, copy them from the tag matching `bf --version`, preserving your adaptations. Then check the brain and update the agent skills:

```bash
cd ~/brain
bf validate
bf eval
bf skills ~/.agents/skills
```

Expect `"valid":true` from validation and `"passed":true` from your retrieval cases; otherwise fix the reported problem first, with [Troubleshooting](troubleshooting.md). `bf skills` updates the skills it installed in that folder: repeat it for each folder you installed them into, such as `~/.claude/skills` for Claude Code. It leaves a folder unchanged and exits 1 when the folder reports `modified` (you edited it), `unmanaged` (it has no BF manifest) or `newer` (a newer `bf` installed a different copy): see [skill installation](agents.md#install-the-skills) before passing `--force`.

`bf init` writes a brain's `AGENTS.md` once. Merge new guidance by hand, as [Refresh brain instructions](agents.md#refresh-brain-instructions) shows. Edit a [watch service](schedule.md#run-watch-as-a-service) or timer when the changelog asks or the `bf` executable moved.

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

Commit `pyproject.toml` and `uv.lock`. Run commands that execute programs, such as `bf update`, through the pin: `uv run --locked bf update` inside the brain, or `uv run --project ~/brain --locked bf update --brain ~/brain` elsewhere. Search, read and checks need no pin: the agent skills run them with your installed `bf` in every brain, and stop to ask when a pin names another major version. Upgrade both together, the pin with `uv add "brain-framework==X.Y.Z"`.

Run a [watch service](schedule.md#run-watch-as-a-service) through the pin too, with absolute paths and the folder of `uv` in its `PATH`:

```bash
uv run --project ~/brain --locked bf watch --json --brain ~/brain
```

`uv run` puts the pinned `bf` first on the `PATH` of the programs it starts, so routines that call `bf` themselves use the same release. Write Git hooks the same way, such as `exec uv run --locked bf run --hook pre-commit` or `exec uv run --locked bf run --hook pre-push -- "$@"`.

Run a pin only in a brain you created or reviewed: it runs with your permissions. `uv run` installs and runs whatever its `uv.lock` names, with the interpreter its `.python-version` names and the settings of its `uv.toml`, and runs an existing `.venv/` as is. Review those files and `pyproject.toml` like `sensors/` and `routines/` before running or upgrading a downloaded or shared brain's pin. Delete a `.venv/` it ships before the first pinned run, and never run the pin of a brain whose Git tracks one (`git ls-files .venv` prints files): every checkout restores it. Search or read a downloaded or shared brain with your installed `bf` ([privacy](privacy.md#running-code)).
