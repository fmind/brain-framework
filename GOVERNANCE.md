# Governance

## Model

The founder maintains Brain Framework and decides. Decisions follow the direction and invariants in [AGENTS.md](AGENTS.md) and happen in public: in issues, pull requests, this file and the [changelog](CHANGELOG.md).

## Maintainers

- Médéric Hurier ([fmind](https://github.com/fmind)): all areas, including releases and security reports.

## Propose a change

Open a [feature request](https://github.com/fmind/brain-framework/issues/new?template=feature.yml) before building something that enlarges the product. Name the user outcome, the evidence from real use and any scope decision below that it crosses. Fixes go straight to a [pull request](CONTRIBUTING.md#submit-a-pull-request).

## Scope decisions

Each decision states why it holds and what evidence would reopen it. This list was first recorded on 2026-11-08; a line without its own `Changed` date dates from then. Propose crossing a decision rather than working around it.

- **Plain files stay the core.** Notes are Markdown, each record is one JSON file, the graph holds only explicit links and search is offline and lexical. Why: a brain stays readable and editable without BF. Reopen: a recurring task plain files cannot serve. See [the overview](docs/docs/index.md).
- **No model inference, provider SDK, plugin framework, importable SDK or record sync.** Each computer runs its own brain. Integrations ship as copy-in stdlib examples (`examples/`), `bf ingest`, declarative `map:`, and the standard starter vocabulary. Why: provider CLIs keep their credentials and the core stays small. Reopen: an integration none of these can express. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0). Changed 2026-11-14.
- **Embeddings stay outside the core.** `bf export --kind items` (with `--observed-since`, `relations` and `attributes`) feeds an external retriever. Why: retrieval stays offline and explainable. Reopen: labelled misses showing that word search plus aliases cannot find the answer. Since [v20.1.0](https://github.com/fmind/brain-framework/releases/tag/v20.1.0). Changed 2026-11-10.
- **Majors ship whenever the design needs them.** Each lists manual upgrade steps, with no compatibility or migration machinery. Why: one current contract is simpler to test and document. Reopen: installations that cannot follow manual steps. See [keep the contract](CONTRIBUTING.md#keep-the-contract).
- **Jobs wake on events and gates.** Declarative `on.event` triggers (canonical `record.*`, `note.changed`, `message.received`, `job.failed`) and optional `gate:` scripts decide when a prompt harness runs, `extract:` / `post:` / `defaults.post` deliver or draft actions, and bounded per-slot wakes follow up. Collection and `bf ingest` only ping `.bf/wakeup` atomically and never wait on `.bf/runtime.sqlite3`. Reopen: a workflow requiring core database coupling or in-core orchestration. Changed 2026-11-14.
- **Identities and relations are never inferred.** Why: a wrong match would mix two subjects' evidence without a trace. Reopen: none planned. See [the overview](docs/docs/index.md).
- **Approvals are not built in.** Bound tools through the harness's permissions (`harness:`), let a read-only assistant emit validated `draft` actions via `emit: action`, or split proposing and acting across two jobs. Why: only the harness's permissions bound what it does. Reopen: a common harness that cannot restrict its tools. See [Assist](docs/docs/assist.md). Changed 2026-11-14.
- **MCP offers `search` and `read` over local stdio.** Why: see [Retrieval](docs/docs/retrieval.md). Reopen: a retrieval need no page or scope can serve.

## Next major

These planned breaks change a contract, so they wait for the next major release, which has no date yet. Never ship one in a minor or patch release.

No breaking changes are currently queued.

## Record a decision

The pull request that adds or changes a decision updates its line here and ends it with the date, such as `Changed 2026-12-01.` The changelog records the decision when it ships.

## Add a maintainer

The founder invites a contributor with sustained, reviewed work in an area to maintain it. Once there are two maintainers, decisions move to lazy consensus: a proposal passes when no maintainer objects within seven days.
