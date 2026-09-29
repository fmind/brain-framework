---
description: Validate saved files and test that recurring questions still retrieve the expected evidence.
---

# Check your brain

Check that files are valid, useful evidence stays findable and programs are healthy. Run these inside your brain:

| Command             | What it checks                                                        |
| ------------------- | --------------------------------------------------------------------- |
| `bf validate`       | Notes, links, actions, record files and the programs `bf.yaml` names. |
| `bf eval`           | Your saved questions still retrieve the expected evidence.            |
| `bf status --check` | The cache and scheduled programs are healthy on this machine.         |

A valid New website note can still omit the reason for its decision: validation accepts its structure, while a retrieval case expecting that reason fails. Neither checks whether the reason is true. Program health depends on this machine's run history, which a teammate's clone does not share.

## Validate

`bf validate` returns `"valid":true`, or a `problems` list naming the brain-relative `file` to repair and its `error`, such as `{"file":"projects/new-website.md","error":"broken link: absent.md"}`. It checks:

- OKF metadata of projects, concepts and `ACTION.md` notes, and action folder names (`YYYY-MM-DD_topic`).
- Every local link, image and `sources` entry, including sections, `bf://` targets in this brain and records named by a provider alias.
- Declared relations: a link naming an undeclared one, a `?rel=` on a link that is not `bf://`, a relation written at the top of frontmatter instead of under `fields:`, and identities outside a relation's `targets`.
- Record files: their names, the fixed record format and stored `fields` against the current `fields:`.
- Each enabled program in `sensors/` or `routines/`: it must be an executable regular file.

It lists at most 200 problems, then `"problems_truncated":true`: fix these and validate again. Targets in other brains appear under [`unresolved`](link-reference.md#across-brains) without making the brain invalid.

## Warnings

`warnings` never make a brain invalid or change the exit code. Validation warns about identities that differ only by letter case, since identities are case-sensitive:

```json
{
  "warning": "identities differ only by letter case",
  "identities": [
    { "identity": "repo:github.com/example/new-website", "files": 12 },
    { "identity": "repo:github.com/Team/new-website", "files": 3 }
  ]
}
```

Each spelling counts the notes and records naming it, most used first. Choose one spelling: fix the sensor emitting the other and recollect, or edit the notes. Validation lists the 200 most used groups, then `"warnings_truncated":true`, and up to 20 spellings per group.

## Retrieval cases

`bf init` creates `evals/retrieval.yaml`, whose three cases find the welcome note, read it through `bf://NAME/concepts/welcome.md` and check an absent topic; `bf eval` returns `"score":"3/3"`. Add your own questions beside it. After [your first decision](getting-started.md#save-a-decision), create `evals/new-website.yaml`:

```yaml
# https://fmind.github.io/brain-framework/docs/checks/
version: 7
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
```

Expect `"score":"3/3"` and `"passed":true`. Run `bf eval` without `--path` to evaluate every suite. To check the original section rather than a search excerpt, add a fourth case at the end of `cases`:

```yaml
- name: read-the-reason
  read: projects/new-website.md#decision
  text: [visitors need a clear explanation before signing up]
```

The suite now reports `"score":"4/4"`. If the note loses the reason, the failed case's `absent` names the missing text; if an expected source leaves the results, `missing` names its ref. Inspect the evidence before changing an assertion.

Choose questions people actually ask, including competing answers and absent topics. The [retrieval example](https://github.com/fmind/brain-framework/tree/main/examples/retrieval) shows competing project budgets, scopes, tags and duplicate records.

## Track ranking changes

A case can keep passing while its answer slips from first to ninth place. Each search case with `expect` therefore reports `rank`: each expected ref's position among the first 50 results, even below the case's `limit`, or `null` when it is missing. The run reports `mrr`, the mean reciprocal rank of those cases. The suite above reports `"mrr":1.0`.

Save a reply outside the brain before you reorganize notes, change a sensor or upgrade BF:

```bash
bf eval --path evals/new-website.yaml > ~/new-website-eval.json
```

To see a regression, rename the `## Next actions` heading to `## Later`, then compare:

```bash
bf eval --path evals/new-website.yaml --baseline ~/new-website-eval.json
```

The command exits 1 with `"score":"3/4"`, `"mrr":0.5` and this comparison:

```json
{
  "regressions": [
    {
      "suite": "evals/new-website.yaml",
      "name": "find-the-next-action",
      "passed": false,
      "rank": { "projects/new-website.md#next-actions": null },
      "baseline": { "passed": true, "rank": { "projects/new-website.md#next-actions": 1 } }
    }
  ],
  "improvements": []
}
```

Rename the heading back before continuing. `regressions` lists cases that passed and now fail, or whose expected refs rank lower; `improvements` lists the reverse. Cases match by suite and name. A lower rank alone does not change the exit status: `bf eval` exits 1 only when a case fails. `--baseline` reads a regular file of at most 16 MiB.

## Suite reference

A suite declares `version: 7`, the brain format, and a list of `cases`. `bf schema --kind eval` prints its JSON Schema (`title: Suite`) for editor validation.

| Field               | What it checks                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------- |
| `name`              | A unique, nonblank case name.                                                                     |
| `query`             | Search words; optional `scope` narrows the evidence and `limit` bounds results.                   |
| `read`              | An exact ref or page; an empty string reads home.                                                 |
| `expect` / `forbid` | Refs or `bf://` addresses that must / must not appear.                                            |
| `text`              | Answer fragments, case-insensitive, present in returned titles and excerpts or in the read reply. |
| `empty: true`       | No returned refs; use instead of `expect` or `text`.                                              |

A case uses either `query` or `read`. Whole-note refs match any of the note's sections; section and record refs match exactly. With related brains, `read` a `bf://NAME/...` address: a plain ref present in two brains fails its case. Evaluation assembles large exact reads from their text pages. Incomplete retrieval fails a case, even an `empty` one.

Every suite is validated before retrieval starts: queries, scopes, read refs, `bf://` addresses and nonblank assertions. An invalid suite stops the run and names the file and field, such as `invalid evals/new-website.yaml: cases.1.scope: ...`. Fix the assertion instead of emptying it.

`bf eval` loads `.yaml` and `.yml` suites recursively in path order, up to 100 suites of 200 cases each. `--path` selects a brain-relative file or folder. For an absent topic, use one distinctive token: hyphens split words, and any query word can match. Use `limit: 1` when the answer must rank first, and pair a search with an exact read when a particular source must hold the answer.
