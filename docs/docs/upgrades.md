---
description: Install or update Brain Framework, match documentation to your version and verify the result.
---

# Install and update

## Install

Brain Framework supports Linux and macOS and requires Python 3.14 or newer. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then let it manage BF's isolated environment:

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

`--python 3.14` selects the tested Python, which uv downloads if needed. Without it, uv may pick a newer Python for which some dependencies publish no prebuilt packages yet. uv remembers the choice for later upgrades. If your shell cannot find `bf`, run `uv tool update-shell` and open a new shell. Then follow [Getting started](getting-started.md).

## Match the docs to your version

This site follows the repository's current code, which can be newer than your installation. `bf --version` names your runtime, and `bf COMMAND --help` lists its options. Copy example sensors, routines and demos from the release tag matching `bf --version`, as the guides' commands do. The brain format, declared as `version:` in `bf.yaml` and each `evals/*.yaml`, is separate from the package version.

## Update

Stop watchers and scheduled collection, read the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md) and back up the idle brain. Then upgrade and confirm the new version:

```bash
uv tool upgrade brain-framework
bf --version
```

For a major release, apply the changelog's upgrade steps now, before collecting again. Then check the brain and update the agent skills:

```bash
cd ~/brain
bf validate
bf eval
bf skills ~/.agents/skills
```

Expect `"valid":true` from validation and `"passed":true` from your retrieval cases; otherwise fix the reported problem first, with [Troubleshooting](troubleshooting.md). `bf skills` updates the skills it installed and never overwrites a folder that reports `modified` (you edited it) or `unmanaged` (it has no `.bf-skill.json` manifest, such as a copy made before 16.0). Either status makes the command exit 1 and leaves that folder unchanged: back up any edits you want to keep, then rerun `bf skills ~/.agents/skills --force`. See [skill installation](agents.md#install-the-skills).

`bf init` writes a brain's `AGENTS.md` once. Merge new guidance by hand, as [Refresh brain instructions](agents.md#refresh-brain-instructions) shows. Regenerate native schedules with `bf schedule` when the changelog asks or the `bf` executable moved.

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

Commit `pyproject.toml` and `uv.lock`, then run commands as `uv run --locked bf ...` inside the brain. Elsewhere, use `uv run --project ~/brain --locked bf ... --brain ~/brain`. Upgrade the pin with `uv add "brain-framework==X.Y.Z"`, and point native schedules at the pinned runtime with `bf schedule --executable ~/brain/.venv/bin/bf`.
