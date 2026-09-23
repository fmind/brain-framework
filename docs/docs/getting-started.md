# Getting started

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then FKF. uv supplies Python 3.14 if needed. If `fkf` is not on PATH, run `uv tool update-shell` and open a new shell.

```bash
uv tool install --python 3.14 'fkf==8.0.1'
fkf --version
```

Create a base. `init` also registers it in `~/.config/fkf/config.yaml`, so `fkf search` finds it from any directory and `fkf update` may run its collectors on this machine.

```bash
fkf init ~/knowledge --name brain
cd ~/knowledge && git init
fkf search welcome
fkf read wiki/welcome.md
```

Write one note per project in `projects/` and reusable knowledge in `wiki/`. Search notices edits by itself. Add a collector only when it answers a question you ask repeatedly; see [collectors](sources.md), then schedule `fkf update`.

## Join a team base

Clone the team repository and register it. Search then covers your personal base and the team base, and labels each result with its base.

```bash
git clone git@github.com:team/knowledge.git ~/team-knowledge
fkf register ~/team-knowledge
fkf search "release process"
```

Registration without `--collect` never runs the team's collectors on your laptop. Team records are usually collected by CI; see [personal and team bases](base.md#personal-and-team-bases).

## Give agents access

Install the [fkf-use skill](https://github.com/fmind/fkf/tree/main/skills/fkf-use) in your host's skill directory, for example `~/.agents/skills/fkf-use/`. Agents then run `fkf search` and `fkf read` from any repository. Hosts that prefer tools can register `fkf mcp` instead; see [MCP](mcp.md).

## Try the example

The [runnable example](https://github.com/fmind/fkf/tree/main/examples/base) contains a fictional project, OKF concept, resumable task, a credential-free collector and retrieval cases. Follow its README in a disposable copy.

## Upgrading from v7

FKF 8 changes the base format: `fkf.yaml` is version 2 without an `id`, records are monthly JSON Lines upserted by id instead of immutable capture files, and refs are readable (`source:id`, `path#section`). Convert a v7 base with a one-off script in that base: write each source's latest record per id to `records/<source>/<YYYY-MM>.jsonl`, rewrite `fkf://…/record:<hash>` citations to `source:id`, set `version: 2` and remove `id`. FKF itself ships no compatibility layer.

## When something is wrong

| Symptom                      | Next step                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------------------- |
| No base selected             | Run inside a base, pass `--base NAME`, or register one with `fkf register PATH`.            |
| A note or record is missing  | `fkf status` lists files the cache skipped and why; `fkf validate` checks the whole base.   |
| A source fails in `update`   | `fkf status` shows its last error and the path of its private stderr log.                   |
| Collection refused           | Trust the base on this machine with `fkf register PATH --collect`.                          |
| Search results look outdated | Another writer held the base; results say `stale`. Retry, or run `fkf build` to start over. |
