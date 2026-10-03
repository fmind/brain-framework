# Runnable retrieval example

Two fictional projects have different budgets. This example checks that questions find the right project's evidence, rank it first among competing notes and duplicate records, preserve scopes and explicit identities, and report absent evidence. Its 23 cases use `bf eval`, without an LLM, credentials or network calls.

Run from the Brain Framework checkout after `uv sync --locked`. This subshell copies the brain, isolates configuration and state, and removes its temporary files on exit:

```bash
(
  set -eu
  bf_checkout=$PWD
  bf_demo=$(mktemp -d)
  bf_demo=$(cd "$bf_demo" && pwd -P)
  trap 'rm -rf -- "$bf_demo"' EXIT
  cp -R examples/retrieval "$bf_demo/brain"
  cd "$bf_demo/brain"
  bf() {
    env -u BF_BRAIN TZ=UTC XDG_CONFIG_HOME="$bf_demo/config" XDG_STATE_HOME="$bf_demo/state" \
      uv run --project "$bf_checkout" bf "$@"
  }
  bf validate
  bf eval
)
```

Expect validation to return `"valid":true`, with 15 notes and six records, and evaluation to return `"passed":true`, `"score":"23/23"` and `"mrr":1.0`. The cases require Atlas's budget of 4200 credits to rank first, excluding Zephyr's competing budget of 900 credits.

`bf.yaml`, `projects/`, `concepts/`, `actions/` and `memories/` form the brain; `evals/retrieval.yaml` holds its questions and expected results. The records are checked in: a disabled `catalog` sensor entry only sets that source's `priority: low`, and nothing is collected. There are no routines or related brains. The example pins UTC and uses fixed dates. Retrieval may refresh the disposable `.bf/` cache; it does not collect or count evaluation reads as user usage.

## Run the test

The normal test suite validates and evaluates a disposable copy of this same example. Run it alone with:

```bash
TZ=UTC uv run pytest -q tests/test_retrieval_example.py
```

For your own brain, adapt its starter `evals/retrieval.yaml` to the questions you need answered; see [Check your brain](../../docs/docs/checks.md). The [workflow example](../brain/README.md) demonstrates collection and actions as well as retrieval.

From the checkout, `uv run bf schema --kind eval` prints the suite's editor schema with `title: Suite`. It rejects a case with both `query` and `read`, or an empty assertion such as `text: [""]`. `bf eval` applies the same structural checks before retrieval, checks each ref as `bf read` does and each query as `bf search` does, and rejects duplicate case names: a `forbid: [tasks#x]` fails with `pages have no sections` instead of passing unnoticed.

## What a case proves

- `query` with `expect` checks that answer-bearing refs appear within `limit` results. Use `limit: 1` when the right evidence must rank first.
- `text` checks case-insensitive literal answer fragments in search titles/excerpts or a read reply. Pair a search case with an exact section or record read when the answer itself matters; avoid full-answer snapshots that fail on harmless edits.
- `forbid` catches plausible distractors or evidence outside the intended scope. `empty: true` checks a genuinely absent topic or ref.
- Any retrieval `problems` or `stale` markers fail the case. Search limits test the requested top results, not exhaustive recall; evaluation assembles an exact reply above 32 KiB from its text pages, but a section keeps the case focused.

These two cases in `evals/retrieval.yaml` check the Atlas budget. Zephyr has a different budget to expose confusion between projects:

```yaml
# https://fmind.github.io/brain-framework/docs/checks/
version: 7
cases:
  - name: atlas-budget-first-result
    query: What is the Atlas launch budget?
    limit: 1
    expect: [projects/atlas.md#budget]
    forbid: [projects/zephyr.md]
    text: [4200 credits]
  - name: read-the-budget-answer
    read: projects/atlas.md#budget
    text: [The Atlas launch budget is 4200 credits.]
```

The first requires the right section in the first result. The second checks the original text. If either fails, inspect that case's diagnostics and its source note before changing the expected answer.

Three more cases reproduce misses measured on real brains, with fictional notes and records:

- `project-next-actions-first` searches `Atlas next actions` among three other projects' Next actions sections and six short action Resume sections that mention Atlas. It requires Atlas's own section first: a section's note title ranks like a heading.
- `one-result-per-document-url` finds the Atlas launch plan once. Its `documents` record and a shorter `catalog` entry share one URL, so search returns the document with `"also":["catalog:atlas-launch-plan"]` instead of two results. The catalog's `priority: low` halves its score, so the full document represents both.
- `day-lists-low-priority-source-by-count` reads the `2026-09-11` page: it lists the document, while the catalog appears only as a count under `sources`, with its `memories/catalog/2026-09-11` page.

Six cases cover the finer search rules:

- `nested-section-budget` asks `What is the Vega budget?` of a portfolio note with a `### Budget` under both `## Orion` and `## Vega`. A section ranks under its parent headings, so `projects/portfolio.md#budget-1`, titled `Portfolio review — Vega — Budget`, answers with 1300 credits, not the empty `#vega` section.
- `tag-outranks-passing-mention` finds Harbor, tagged `accessibility`, above Cobalt, whose introduction mentions accessibility once: tags rank like headings.
- `whole-note-preview` asks `What is Atlas?`. The note matches by its title and has no introduction, so its excerpt previews its first section.
- `word-prefix` finds Borealis, which "synchronizes" field reports, with `synchro*`.
- `newest-of-identical-meetings` lists the later of two identical `calendar` status events first: equal scores list the newest item first.
- `date-range-page` reads `2026-09-10..2026-09-14`, an inclusive range of local days: it lists the launch plan and both reviews, but neither status event, on 2026-09-08 and 2026-09-15.

Each search case with `expect` reports `rank`, the position of each expected ref within the first 50 results, even below the case's `limit`, and the run reports `mrr`, the mean reciprocal rank. Every case ranks an expected ref first, hence `1.0`; the launch plan outranks a glossary definition sharing two of its three words, because a passage matching all the query's words gains 20%. To check a ranking change, save a reply with `bf eval > ../baseline.json` before it and run `bf eval --baseline ../baseline.json` after it: `regressions` lists cases that fail or rank lower, and `improvements` those that pass or rank higher. See [track ranking changes](../../docs/docs/checks.md#track-ranking-changes).

These assertions evaluate retrieval, not generated prose, semantic equivalence or source truth. Search `text` is matched across all returned excerpts, so it does not bind an answer fragment to a particular hit when several hits are allowed. A passing score covers only these cases and this corpus; it does not establish performance on arbitrary questions, live freshness or private brains.

## Add a useful case

Capture a real retrieval miss using synthetic evidence. Preserve the question's useful difficulty: competing answers, scope, expected rank or missing evidence. Add a short expected answer fragment and its exact source ref; keep distractors. Run the focused pytest command before changing ranking, then again after the fix. Update the documented and tested case count and `mrr` when adding a case. Never change expectations merely to make a regression pass.

Keep parser errors, process cancellation, pagination mechanics and evaluator failure handling in `tests/`. Keep curated question-to-evidence cases alongside their runnable example. Do not add model grading, another evaluator or a new suite format; [the existing contract](../../docs/docs/checks.md#suite-reference) owns the fields. Personal brains keep their own `evals/` suites, run with `bf eval` inside the brain.
