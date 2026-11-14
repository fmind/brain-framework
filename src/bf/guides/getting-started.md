# Getting started — install, initialize, register, skills, and MCP

Set up Brain Framework locally, initialize your brain directory, register it cleanly, install the `/bf` agent skill, and connect read-only MCP retrieval.

## Install `bf`

Install the CLI globally with `uv` on Python 3.14+:

```bash
uv tool install --python 3.14 brain-framework
```

Include optional MCP server support when connecting IDE or desktop agent hosts:

```bash
uv tool install --python 3.14 'brain-framework[mcp]'
```

Verify the installation with `bf --version`. Upgrade cleanly with `uv tool upgrade brain-framework`.

## Initialize a brain

Create a new brain workspace:

```bash
bf init ~/brain
cd ~/brain
```

`bf init` writes:

- `bf.yaml`: minimal brain configuration declaring schema `version` with no required `fields:` boilerplate
- `.gitignore`: excludes `/.bf/`, `/memories/`, `/actions/`, `/logs/`, `/originals/`, and `/inputs/`
- `AGENTS.md`: concise single-brain instructions pointing agents to `/bf`, `bf guide`, `bf read`, `bf search`, and safe note authoring
- `concepts/index.md`, `concepts/welcome.md`, `projects/`, and `evals/retrieval.yaml`

Run `bf validate` to confirm the starter brain is valid, then `bf read` to inspect the home page.

## Register your brain

Registering a brain stores its name and path in `~/.config/bf/config.yaml` (never inside the brain repo):

```bash
bf register ~/brain
bf register ~/brain --name work --default
```

Registration lets you target the brain from any directory via `--brain NAME` or the user `default` entry in `~/.config/bf/config.yaml`. It also marks `"registered": true` in `bf status`. Remove an entry later with `bf unregister NAME`.

**Security boundary for pinned runtimes**: run the global `bf` binary on `PATH` by default. Run `uv run --locked bf ...` only when a brain contains `pyproject.toml`/`uv.lock` AND `bf status` shows `"registered": true`. Never run `uv run` on unregistered brains or downloaded clones so untrusted code never runs.

## Install the `/bf` skill

Install the packaged `bf` skill into the brain (`<brain>/skills/bf` linked to `<brain>/.agents/skills/` and `<brain>/.claude/skills/`), or globally into `~/.agents/skills/bf`:

```bash
bf skill install
bf skill install --global
```

Use `--check` to verify skill state without writing and `--force` to replace edited copies. Inspect `bf skill install --help` for options. Once installed, agents can invoke `/bf` and call `bf guide TOPIC` on demand.

## Connect an offline MCP client

`bf mcp` starts a local stdio MCP server exposing strictly two offline tools: `search` and `read`. It never runs sensors, routines, assistants, channels, or network calls.

Configure your agent host to spawn:

```bash
bf mcp --brain ~/brain
```

Or omit `--brain` when running inside the brain or relying on `BF_BRAIN` / user `default` registration. Test `search` for `"welcome"` and `read` on `concepts/welcome.md` to confirm access.
