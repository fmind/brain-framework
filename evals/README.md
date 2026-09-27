# Retrieval evaluations

Retrieval evaluations primarily belong to each user's brain: `bf init` creates `tests/` for technical tests and `evals/retrieval.yaml` with runnable starter checks. Run `bf eval` inside that brain and adapt its suites to the questions its owner needs answered; see [Check your brain](../docs/docs/checks.md).

This repository's `evals/` is a separate fictional regression corpus for framework development. `tests/` checks technical behavior and failure boundaries; this corpus checks whether realistic questions retrieve expected evidence. Both run in `mise run all`; neither needs an LLM. The example brain retains its own onboarding cases.

```bash
mise run eval
```

This validates the corpus, then runs `bf eval --brain ./evals --path retrieval.yaml`. The explicit `./evals` path bypasses registered brain names and `BF_BRAIN`. `bf.yaml`, `projects/`, `concepts/` and `memories/` form the corpus; `retrieval.yaml` holds its questions and expected results. There are no sensors, routines, related brains or network calls. The task pins UTC and uses fixed dates. Retrieval may refresh the ignored `evals/.bf/` cache; it does not collect or count evaluation reads as user usage.

## What a case proves

- `query` with `expect` checks that answer-bearing refs appear within `limit` results. Use `limit: 1` when the right evidence must rank first.
- `text` checks case-insensitive literal answer fragments in search titles/excerpts or a read reply. Pair a search case with an exact section or record read when the answer itself matters; avoid full-answer snapshots that fail on harmless edits.
- `forbid` catches plausible distractors or evidence outside the intended scope. `empty: true` checks a genuinely absent topic or ref.
- Any retrieval `problems` or `stale` markers fail the case. Search limits test the requested top results, not exhaustive recall; oversized exact replies need a smaller section.

These two cases in `retrieval.yaml` check the Atlas budget. Zephyr has a different budget to expose confusion between projects:

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

Capture a real retrieval miss using synthetic evidence. Preserve the question's useful difficulty: competing answers, scope, expected rank or missing evidence. Add a short expected answer fragment and its exact source ref; keep distractors. Run `mise run eval` before changing ranking, then again after the fix. Never change expectations merely to make a regression pass.

Keep parser errors, process cancellation, pagination mechanics and evaluator failure handling in `tests/`. Keep curated question-to-evidence cases here. Do not add model grading, another evaluator or a new suite format; [the existing contract](../docs/docs/checks.md#suite-reference) owns the fields. Personal brains keep their own `evals/` suites, run with `bf eval --brain PATH`.
