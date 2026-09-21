# Retrieval and context

Search uses SQLite FTS5 with literal Unicode terms, weighted title matching, explicit identity matches and a preference for authored notes that scales with lexical relevance. Stable URI ordering breaks ties. Ordinary search selects the latest known capture of each source/id before matching and event-time filtering. An old capture cannot win because its wording matches better. Use `--history` to search all captures; exact captured-record references remain readable in either mode.

```bash
fkf find "retention decision" --base ~/knowledge --limit 10
fkf context "repo:example/project" --base ~/knowledge --budget 850
fkf read projects/example.md#reason --base ~/knowledge
```

Indexed retrieval requires a ready index. Missing, stale or corrupt indexes return an actionable error: run `fkf build` for the selected base. A ready index is opened read-only in place, so a concurrent build or collection cannot disturb the generation already open. Reads never build an index. Exact Markdown paths and capture-file paths remain readable without an index for recovery; aliases and record references require the index.

The budget applies to the entire compact JSON response and its final newline. For MCP it also includes both text and structured representations and the tool-result wrapper, so the same budget may deliver fewer items than the CLI. It includes selected excerpts, references, omitted-candidate count, budget, cache diagnostic and the untrusted-content notice. Four bytes per unit is an exact byte allowance, not a tokenizer estimate. Exact reads are bounded but never silently shortened.

## Acceptance cases

```yaml
# https://fmind.github.io/fkf/
version: 1
cases:
  - name: recover-decision
    query: retention decision
    expect: [projects/example.md]
    excerpts:
      projects/example.md: [Keep historical evidence]
    reads:
      projects/example.md: [Keep historical evidence]
    budget: 850
    limit: 10
  - name: unrelated
    query: absent-unique-topic
    empty: true
```

Cases may also name forbidden references. Evaluate meaningful questions and answer-bearing text, including absence, stale decisions and contradictions. A passing fixture suite is evidence about those cases, not universal agent productivity.

Source and time constraints are explicit: `--source NAME`, `--after TIME` (inclusive), and `--before TIME` (exclusive). Timestamps include a timezone and normalize to UTC microseconds. Undated notes remain eligible within a time window; records without a time do not satisfy a nonempty time bound. `--order recent` sorts by evidence time before relevance. Natural-language questions do not infer sources, dates, or temporal order.

Replies include the persistent base identity. Each result carries a local `uri` and a base-qualified exact `ref`; section results also name their `fragment`. Cite `ref` for evidence. The stable `alias` navigates the newest stored observation and is not immutable proof. Original capture bytes remain readable after new captures. Snapshot labels describe stored recency, not decision validity or provider freshness.

Evaluation cases accept the same `source`, `after`, `before`, `order`, and `history` constraints. Keep expected references and answer-bearing excerpt/read assertions grounded in durable evidence.

Excerpts retain short evidence in full. Long notes keep their authored introduction alongside a matching passage, so an explicit decision is not lost when its query terms occur later. Context shortens excerpts to fit remaining bytes, marking omitted passage text with an ellipsis; exact `read` never shortens evidence. Lexical discovery drops only English function words, keeps single-character and subject terms such as `resume` or `active`, and preserves the literal query when cleanup would leave no terms. Case and diacritics never separate a query from its evidence: `reunion` matches `réunion` in matching, title weighting and excerpt selection.

Note fragments use parsed Markdown headings, including Setext headings. Code fences do not create headings. Repeated headings receive `-1`, `-2`, and subsequent suffixes. Section reads preserve original line endings. The default evaluation budget is the same 850 units as ordinary CLI context. The repository’s synthetic acceptance corpus exercises delivery at that default and smaller budgets.

## Recover current decisions and their history

Start with the authored decision note and its supporting exact references. The note should state the current decision, reason, effective date when known, review date, and what it supersedes. An observation's event time, its capture time, and a decision's validity are distinct; the newest capture alone establishes none of the owner's policy.

```bash
fkf context "decision:retention" --base ~/knowledge
fkf find "previous retention policy" --base ~/knowledge --history
fkf context "retention" --base ~/knowledge --history --before 2026-09-01T00:00:00Z
```

History includes every matching capture, so narrow the source or event-time window where useful. Time bounds always filter record event time, not capture time; history is not an as-of reconstruction. Authored notes remain eligible and should label superseded guidance explicitly. Reading a whole collection returns its original envelope without inferred validity labels.

## Knowledge and source structure filters

`find` and `context` accept `--type`, `--status` and `--within`. MCP names the type argument `note_type`; evaluation cases use `type`, `status` and `within`. Accepted/current metadata increases the preference for an authored decision, while explicit supersession and the `History` heading keep historical guidance out of ordinary results. H2+ sections are retrieved individually, and their exact references never claim evidence from another section.

Use `find '*' --within CONTAINER_ID` to browse explicit transitive membership. The latest complete source snapshot can retire absent members; windowed captures cannot. `build` also writes `indexes/structures.json`. See [base layout and metadata](base.md).
