# Getting started

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install FKF on Linux or macOS. uv supplies Python 3.14 if needed. If `fkf` is not on PATH, run `uv tool update-shell` and open a new shell.

```bash
uv tool install --python 3.14 'fkf==7.0.0'
fkf --version
```

From a development checkout, use `uv sync --locked` and prefix commands with `uv run`.

```bash
fkf init ~/knowledge
fkf build --base ~/knowledge
fkf context "project decisions" --base ~/knowledge --budget 850
fkf read wiki/welcome.md --base ~/knowledge
```

Write useful decisions, constraints and next actions in Markdown under projects/ and wiki/. Add a source only when it answers a recurring question. Review its adapter and configuration before running an explicit collection.

Keep collectors in `sources/`, maintenance commands in `scripts/`, and their tests and synthetic fixtures in `tests/`. These folders are created by `init` and are not indexed. Root `inputs/` is optional original storage; task-local `inputs/` belongs to its resumable task. See [base layout](base.md) for all folder conventions.

## Upgrading from v6

v7 replaces the Go implementation, command surface and base format. Preserve the old base and executable, initialize a separate v7 base, and deliberately copy or convert the evidence you need using base-owned tools. FKF provides no in-place migration or compatibility commands. Skills and source examples are separate repository resources; installing the Python package does not install them.

## Agent workflows

The [skills catalog](https://github.com/fmind/fkf/tree/main/skills) contains reusable `fkf-use`, `fkf-learn` and `fkf-maintain` packages. Copy selected packages into your base’s canonical `skills/` directory and expose reviewed packages through project-local `.agents/skills/`, using the host’s supported links or copies. A host-wide installation is a separate choice. Use the base-local instructions to select its installed executable. See the [base layout](base.md) and [MCP setup](mcp.md).

## Try a complete example

The [runnable example](https://github.com/fmind/fkf/tree/main/examples/base) contains a fictional project, OKF concept, resumable task, local fake collector and acceptance cases. Follow its README from a disposable copy; no credentials or live provider are needed. Run `fkf build` after authored changes before using indexed retrieval.

## Common recovery steps

| Symptom                           | Next step                                                                                                                                |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| No base selected                  | Pass `--base PATH` after the command, or work inside a base.                                                                             |
| Index missing, stale or corrupt   | Run `fkf build --base PATH`, then retry retrieval.                                                                                       |
| No useful matches                 | Try `fkf find` with literal subject words and inspect your authored notes; nothing is fetched automatically.                             |
| `eval` cannot find `queries.yaml` | Add owner-authored [acceptance cases](context.md#acceptance-cases); initialization does not invent evaluation questions.                 |
| A source fails during `update`    | Inspect the reviewed adapter and its provider environment; retry only within collection authority. Successful captures remain preserved. |
