# Check your brain

Check that files are valid, useful evidence is findable and collection is healthy. Run these inside your brain:

| Command             | What it establishes                                              |
| ------------------- | ---------------------------------------------------------------- |
| `bf validate`       | Notes, links, actions and record files have valid structure.     |
| `bf eval`           | Your chosen questions still retrieve the expected evidence.      |
| `bf status --check` | The local cache and scheduled programs meet their health checks. |

For example, a valid New website note can still omit the reason for its decision. `bf validate` accepts its structure; a retrieval case that expects that reason fails. Neither check establishes whether the reason is true. Source health also depends on the [collecting machine's history](schedule.md#timing-and-health), which a teammate's clone does not inherit.

Keep technical tests for your sensors and routines in `tests/`, and retrieval questions with expected evidence in `evals/`. `bf eval` uses deterministic assertions, with no LLM or provider execution: it checks evidence retrieval rather than grading generated answers.

`bf init` creates a starter `evals/retrieval.yaml`. Its three cases search for the welcome note, read its answer and check an absent topic. `bf eval` should return `"score":"3/3"`. Add cases for your own notes as shown below; existing brains can add `evals/` without reinitializing.

## Retrieval cases

Complete [your first decision](getting-started.md#save-a-decision) before using these cases.

Add `evals/new-website.yaml` in `~/brain`, keeping any existing suites. These cases check the reason, the next task and a deliberately absent topic:

```yaml
# https://fmind.github.io/brain-framework/docs/checks/
version: 5
cases:
  - name: find-the-reason
    query: visitors clear explanation
    expect: [projects/new-website.md#decision]
    text: [visitors need a clear explanation before signing up]
  - name: find-the-next-action
    query: Draft product page
    expect: [projects/new-website.md#next-actions]
    text: [Draft the product page]
  - name: avoid-an-unrelated-answer
    query: bfabsentevidence9c4f2a7d
    empty: true
```

```bash
bf eval --path evals/new-website.yaml
bf validate
```

Expect `"score":"3/3"` and `"passed":true` from this suite, then `"valid":true` from validation. Run `bf eval` without `--path` to evaluate all saved suites.

To check the original section rather than a search excerpt, append this fourth case under `cases`:

```yaml
# https://fmind.github.io/brain-framework/docs/checks/
- name: read-the-reason
  read: projects/new-website.md#decision
  text: [visitors need a clear explanation before signing up]
```

Run the same suite again; it should now report `"score":"4/4"`. If the note loses the reason, `absent` in the failed case identifies the missing text. If an expected source disappears from the results, `missing` identifies its ref. Inspect the returned source before changing the assertion.

Use questions people need answered. A small team pilot can start with one project and three cases; ask a teammate to read the returned evidence from a fresh clone and check that it answers the questions.

## Suite reference

A case uses either `query` or `read`. Keep assertions short and stable:

| Field               | What it checks                                                                  |
| ------------------- | ------------------------------------------------------------------------------- |
| `query`             | Search words; optional `scope` narrows the evidence and `limit` bounds results. |
| `read`              | An exact ref or page; an empty string reads home.                               |
| `expect` / `forbid` | Refs or BF addresses that must appear / must not appear.                        |
| `text`              | Answer fragments present in the reply.                                          |
| `empty: true`       | No returned refs; use instead of `expect` or `text`.                            |

Whole-note refs match any section; section and record refs match exactly. Prefer qualified addresses across brains.

`text` must occur in returned search titles or excerpts, or in a read reply. `empty: true` requires no returned refs. Incomplete retrieval fails even for an empty case; a read needing chunk assembly also fails, so target a smaller section.

For an absent query, use one distinctive alphanumeric token that does not occur in the evidence. Hyphens split words, and search matches any query word: `nonexistent-topic` can match an ordinary note about a topic.

`bf eval` loads `.yaml` and `.yml` suites recursively in path order. Select a file or directory with `--path evals/retrieval.yaml` or `--path evals/team`. When a real question misses, add a case before improving the note or sensor.

Use `limit: 1` when the expected evidence must rank first. Search `text` assertions apply across all returned titles and excerpts, so pair a search with an exact read when a particular source must contain the answer. Include competing answers, scope and absent topics. A passing score measures only the chosen evidence and assertions; it does not measure semantic equivalence or prove a claim true.
