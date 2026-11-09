# Governance

## Model

The founder maintains Brain Framework and decides. Decisions follow the direction and invariants in [AGENTS.md](AGENTS.md) and happen in public: in issues, pull requests, this file and the [changelog](CHANGELOG.md).

## Maintainers

- Médéric Hurier ([fmind](https://github.com/fmind)): all areas, including releases and security reports.

## Propose a change

Open a [feature request](https://github.com/fmind/brain-framework/issues/new?template=feature.yml) before building something that enlarges the product. Name the user outcome, the evidence from real use and any scope decision below that it crosses. Fixes go straight to a [pull request](CONTRIBUTING.md#submit-a-pull-request).

## Scope decisions

Each decision states why it holds and what evidence would reopen it. This list was first recorded on 2026-11-08; a line without its own `Changed` date dates from then. Propose crossing a decision rather than working around it.

- **Plain files stay the core.** Notes are Markdown, each record is one JSON file, the graph holds only explicit links and search is offline and lexical. Why: a brain stays readable and editable without BF. Reopen: a recurring task plain files cannot serve. See [what stays authoritative](docs/docs/concepts.md#what-stays-authoritative).
- **No model inference, provider SDK, plugin framework, importable SDK or record sync.** Each computer runs its own brain. Integrations ship as the copy-in sensor kit, the standard vocabulary and `bf collect --input`. Why: provider CLIs keep their credentials and the core stays small. Reopen: an integration none of these can express. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0).
- **Embeddings stay outside the core.** `bf export --kind items` feeds an external retriever. Why: retrieval stays offline and explainable. Reopen: labelled misses showing that word search plus aliases cannot find the answer. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0).
- **Majors ship whenever the design needs them.** Each lists manual upgrade steps, with no compatibility or migration machinery. Why: one current contract is simpler to test and document. Reopen: installations that cannot follow manual steps. See [keep the contract](CONTRIBUTING.md#keep-the-contract).
- **Assistants wake on gates.** A gate decides when the harness runs, a `{say, wake}` reply whether it speaks, one bounded wake follows up and `post` names one channel. No change feed: collection never waits on the assistant queue. Reopen: an event no gate can find in collected records. Since [v20.0.0](https://github.com/fmind/brain-framework/releases/tag/v20.0.0) and [v21.0.0](https://github.com/fmind/brain-framework/releases/tag/v21.0.0).
- **Identities and relations are never inferred.** Why: a wrong match would mix two subjects' evidence without a trace. Reopen: none planned. See [what BF does not do](docs/docs/concepts.md#what-bf-does-not-do).
- **Approvals are not built in.** Use the harness's permissions, or let a read-only assistant propose and a second one act on your answer. Why: only the harness's permissions bound what it does. Reopen: a common harness that cannot restrict its tools. See [keep prompts safe](docs/docs/prompts.md#keep-prompts-safe) and [propose, then act](docs/docs/prompts.md#propose-then-act).
- **MCP offers `search` and `read` over local stdio.** Why: see [why local stdio and two tools](docs/docs/mcp.md#why-local-stdio-and-two-tools). Reopen: a retrieval need no page or scope can serve.

## Next major

These planned breaks change a contract, so they wait for the next major release, which has no date yet. Never ship one in a minor or patch release.

1. A schedule that sets both `every` and `at` will be rejected.
1. An entity or alias at `bf://NAME/actions/SLUG` will be rejected, because that address reads an action.
1. `truncate_text` will merge into `max_text_bytes`.
1. The `--no-*` forms Typer adds to boolean flags, such as `--no-dry-run`, will be removed.
1. Cut record text will carry only `partial`: the `truncated` attribute goes away.
1. The example `github-history.py` sensor will print singular `kind` values, such as `commit` instead of `commits`.

## Record a decision

The pull request that adds or changes a decision updates its line here and ends it with the date, such as `Changed 2026-12-01.` The changelog records the decision when it ships.

## Add a maintainer

The founder invites a contributor with sustained, reviewed work in an area to maintain it. Once there are two maintainers, decisions move to lazy consensus: a proposal passes when no maintainer objects within seven days.
