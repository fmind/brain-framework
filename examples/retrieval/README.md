# Runnable retrieval example

Two fictional projects have different budgets. This example checks that questions find the right project's evidence, preserve scopes and explicit identities, and report absent evidence. Its 14 cases use `bf eval`, without an LLM, credentials or network calls.

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

Expect validation to return `"valid":true`, with three notes and two records, and evaluation to return `"passed":true` and `"score":"14/14"`. The cases require Atlas's budget of 4200 credits to rank first, excluding Zephyr's competing budget of 900 credits.

`bf.yaml`, `projects/`, `concepts/` and `memories/` form the brain; `evals/retrieval.yaml` holds its questions and expected results. There are no sensors, routines or related brains. The example pins UTC and uses fixed dates. Retrieval may refresh the disposable `.bf/` cache; it does not collect or count evaluation reads as user usage.

## Run the test

The normal test suite validates and evaluates a disposable copy of this same example. Run it alone with:

```bash
TZ=UTC uv run pytest -q tests/test_retrieval_example.py
```

For your own brain, adapt its starter `evals/retrieval.yaml` to the questions you need answered; see [Check your brain](../../docs/docs/checks.md). The [workflow example](../brain/README.md) demonstrates collection and actions as well as retrieval.

From the checkout, `uv run bf schema --kind eval` prints the suite's editor schema with `title: Suite`. It rejects a case with both `query` and `read`, or an empty assertion such as `text: [""]`. `bf eval` applies the same structural checks before retrieval and also rejects duplicate case names.

## What a case proves

- `query` with `expect` checks that answer-bearing refs appear within `limit` results. Use `limit: 1` when the right evidence must rank first.
- `text` checks case-insensitive literal answer fragments in search titles/excerpts or a read reply. Pair a search case with an exact section or record read when the answer itself matters; avoid full-answer snapshots that fail on harmless edits.
- `forbid` catches plausible distractors or evidence outside the intended scope. `empty: true` checks a genuinely absent topic or ref.
- Any retrieval `problems` or `stale` markers fail the case. Search limits test the requested top results, not exhaustive recall; an exact reply above 65,536 characters arrives in JSON chunks, so read a smaller section instead.

These two cases in `evals/retrieval.yaml` check the Atlas budget. Zephyr has a different budget to expose confusion between projects:

```yaml
# https://fmind.github.io/brain-framework/docs/checks/
version: 5
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

These assertions evaluate retrieval, not generated prose, semantic equivalence or source truth. Search `text` is matched across all returned excerpts, so it does not bind an answer fragment to a particular hit when several hits are allowed. A passing score covers only these cases and this corpus; it does not establish performance on arbitrary questions, live freshness or private brains.

## Add a useful case

Capture a real retrieval miss using synthetic evidence. Preserve the question's useful difficulty: competing answers, scope, expected rank or missing evidence. Add a short expected answer fragment and its exact source ref; keep distractors. Run the focused pytest command before changing ranking, then again after the fix. Update the documented and tested case count when adding a case. Never change expectations merely to make a regression pass.

Keep parser errors, process cancellation, pagination mechanics and evaluator failure handling in `tests/`. Keep curated question-to-evidence cases alongside their runnable example. Do not add model grading, another evaluator or a new suite format; [the existing contract](../../docs/docs/checks.md#suite-reference) owns the fields. Personal brains keep their own `evals/` suites, run with `bf eval` inside the brain.
