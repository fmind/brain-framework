---
icon: lucide/package
description: Install or update Brain Framework, match documentation to your version and verify the result.
---

# Install and update

## Install

Brain Framework runs on Linux and macOS with Python 3.14+. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install BF in an isolated tool environment:

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

Run `uv tool update-shell` if `bf` is not yet on `PATH`, then follow [Getting started](getting-started.md). For [MCP](mcp.md) support, install `'brain-framework[mcp]'`.

- **PyYAML without libyaml**: `uv tool install --reinstall --python 3.14 brain-framework` (include `[mcp]` if used).
- **SQLite older than 3.35.0**: `uv tool install --reinstall --managed-python --python 3.14 brain-framework`.

## Match the docs to your version

This documentation tracks `main`. Check `bf --version` and `bf COMMAND --help` for your installed CLI, and copy example scripts (`_sensor.py`, `_bf.py`, sensors, routines, channels, hooks) from the matching `v$(bf --version)` tag.

## Versioning and stability

Releases follow `MAJOR.MINOR.PATCH` ([contract rules](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md#keep-the-contract)):

- **Within a major**: `search`, `read` and `status` [reply schemas](retrieval.md#reply-schemas) only gain optional fields or narrow values; `bf.yaml` and `evals/*.yaml` only widen accepted inputs; brain format `version` (currently `8`) stays constant.
- **Across majors**: breaking CLI, configuration, queue or reply changes batch cleanly without legacy shims and list manual upgrade steps in the [changelog](../changelog.md) ([queued breaks](https://github.com/fmind/brain-framework/blob/main/GOVERNANCE.md#next-major)). Format `version` increments only when stored brain files require conversion.

## Update

1. **Before upgrading**: stop `bf watch` / timers, read the [changelog](../changelog.md), drain `bf queue` on the current release if assistant/channel schemas or settings change, and back up both the brain and `$XDG_STATE_HOME/bf/runtime/*/queue.sqlite3` while idle.
2. **Upgrade the package**:

```bash
uv tool upgrade brain-framework
bf --version
```

3. **Apply release steps**: update `bf.yaml` settings, refresh copied examples (including sibling `_sensor.py` and `_bf.py` helpers) from `v$(bf --version)`, and refresh `AGENTS.md` ([refresh brain instructions](agents.md#refresh-brain-instructions)) and `# yaml-language-server: $schema=...` headers if desired.
4. **Verify and refresh skills**:

```bash
cd ~/brain
bf validate
bf eval
bf status --check
bf skills ~/.agents/skills
```

Repeat `bf skills` for each installed host directory (`~/.claude/skills`, or `bf skills skills --link` inside a personal brain; pass `--force` only after backing up local skill edits). Restart watchers and MCP hosts afterwards.

## Pin a brain's runtime

Pin an exact BF version inside a brain with `uv` so execution commands and team clones run identical releases:

```bash
cd ~/brain
uv init --bare --python 3.14
uv add "brain-framework==$(bf --version)"
echo /.venv/ >> .gitignore
uv run --locked bf --version
```

Commit `pyproject.toml` and `uv.lock` (never `.venv/`). Run execution commands through `uv run --locked bf update` (or `uv run --project ~/brain --locked bf watch --json --brain ~/brain` in services and `.git/hooks`), while keeping interactive retrieval and skills on your globally installed `bf` ([running code](privacy.md#running-code)). Always audit `pyproject.toml`, `uv.lock`, `.python-version` and `uv.toml` and delete any shipped `.venv/` before running a shared brain's pin.
