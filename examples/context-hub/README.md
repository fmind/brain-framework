# Four-tool walkthrough

Answer one question across Google Workspace, Jira, GitHub, and Google Cloud fixtures: **"Can the New website launch, what remains, and why did we choose one page?"** Only the initial `git clone` uses the network; no credentials or LLM are required.

`tests/test_context_hub.py` executes these walkthrough commands on a disposable copy of this folder.

| Path                      | Purpose                                                                                                     |
| ------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `bf.yaml`                 | Declares the `project:` relation target constraint (`project:`) and four `snapshot` jobs (`emit: records`). |
| `evals/retrieval.yaml`    | Ten retrieval cases: `bf eval` reports `"score":"10/10"`.                                                   |
| `fixtures/*.json`         | Offline JSON fixtures for `workspace`, `jira`, `github`, `gcloud`, plus `jira-done.json`.                   |
| `projects/new-website.md` | Starter project note tracking the launch decision.                                                          |
| `prompts/watcher.md`      | Watcher assistant prompt; the walkthrough copies [records-gate.py](../routines/records-gate.py) here.       |
| `sensors/demo.py`         | Normalizes `fixtures/TOOL.json` into BF records (`@type`, `relations`, `attributes`) without network calls. |

## Get the example

```bash
context_demo=$(mktemp -d)
git clone --quiet --depth 1 --branch "v$(bf --version)" https://github.com/fmind/brain-framework.git "$context_demo/source"
cp -R "$context_demo/source/examples/context-hub" "$context_demo/brain"
cd "$context_demo/brain"
```

Review `bf.yaml` and `sensors/demo.py` before running.

## Collect four tools

```bash
bf collect workspace
bf collect jira
bf collect github
bf collect gcloud
bf read project:new-website
```

Each collection reports `"records":1`. Reading `project:new-website` opens the project note and groups all four normalized records under the `project` backlink relation:

```json
{
  "relation": "project",
  "total": 4,
  "items": [
    { "ref": "gcloud:deployment", "kind": "record", "@type": "Deployment" },
    { "ref": "github:implementation", "kind": "record", "@type": "PullRequest" },
    { "ref": "jira:review", "kind": "record", "@type": "Issue" },
    { "ref": "workspace:brief", "kind": "record", "@type": "Brief" }
  ]
}
```

Read the blocking issue and verify the brain:

```bash
bf read jira:review
bf validate
bf eval
```

`bf read jira:review` shows keyboard navigation still blocks launch; validation reports `"valid":true` and evaluation `"score":"10/10"`.

## How the four tools share one project

Each tool names its project and status differently. `sensors/demo.py` maps them into shared `@type`, `relations.project`, and `attributes` (`kind`, `status`), while `bf.yaml` restricts `fields.project.targets` to `project:` ([reference](../../docs/docs/reference.md)):

| Tool      | Project pointer                 | Status pointer      | Normalized `@type` and `attributes.kind`      |
| --------- | ------------------------------- | ------------------- | --------------------------------------------- |
| Workspace | `attributes.project`            | `attributes.state`  | `Brief`, `kind: brief`, `status`              |
| Jira      | `attributes.workstream`         | `attributes.status` | `Issue`, `kind: issue`, `status`              |
| GitHub    | `attributes.repository_project` | `attributes.state`  | `PullRequest`, `kind: pull-request`, `status` |
| Gcloud    | `attributes.service_project`    | `attributes.health` | `Deployment`, `kind: deployment`, `status`    |

```yaml
# https://fmind.github.io/brain-framework/docs/reference/
version: 11
name: context-demo
fields:
  project:
    description: Project explicitly identified by the source.
    targets: ["project:"]
jobs:
  jira:
    run: [sensors/demo.py, jira, "{{end}}"]
    emit: records
    mode: snapshot
```

When writing custom jobs against raw CLI JSON output (`gh`, `gws`, `acli`, `gcloud`), declarative `map:` in `bf.yaml` can extract those pointers without a Python script ([jobs](../../docs/docs/jobs.md)).

## Record the conclusion

Save the verified state in `projects/new-website.md`, dated today:

```bash
cat > projects/new-website.md <<EOF
---
type: project
status: draft
updated: $(date +%F)
aliases: [project:new-website]
summary: Launch a product website that explains the product before signup.
---

# New website

## Decision

Start with a single product page: visitors need a clear explanation before signing up. Evidence: [Workspace brief](workspace:brief).

## Launch review

Hold public launch. The [implementation](github:implementation) is merged and the [preview](gcloud:deployment) is healthy, but the [launch review](jira:review) still blocks launch on the keyboard navigation check.

## Next actions

- [ ] Run the keyboard navigation check.
EOF
bf read
```

Home lists the project with its `next` task and no `review` flag (`changed` stays empty because projects report under `projects`).

## Hear of each change once

A prompt job can watch record changes offline with `on: { event: { kind: record, source: jira } }` (`record` expands to `record.added`, `record.changed`, and `record.removed`), or use a custom `gate:` script ([records-gate.py](../routines/records-gate.py)) when filtering attributes with `--where` ([assist](../../docs/docs/assist.md)):

```bash
mkdir -p routines
cp "$context_demo/source/examples/routines/records-gate.py" routines/
cat >> bf.yaml <<'EOF'
  watcher:
    prompt: prompts/watcher.md
    harness:
      [
        printf,
        "---\ntype: action\nstatus: draft\nupdated: 2026-09-27\nsummary: Re-check website launch review after Jira change.\n---\n\n# Launch review check\n\n## Decision\n\nReview [Jira issue](jira:review) against the launch decision.\n",
      ]
    emit: action
    on:
      every: 1s
    gate:
      [python3, routines/records-gate.py, jira, --since, "{{start}}", --until, "{{end}}", --state, "{{state}}"]
EOF
bf validate
bf update --job watcher
sleep 1
bf update --job watcher
```

`printf` stands in for an agent harness and `emit: action` writes a `draft` `ACTION.md`. Validation stays `"valid":true`; the first update finds `jira:review`, runs the watcher (`"status":"ran"`, returning an `"action"` path), while the second finds nothing new and skips starting a harness (`"status":"gated"`).

## Follow a change

Move the Jira fixture to `Done` (`attributes.updated` advances while event `time` stays fixed; [jobs](../../docs/docs/jobs.md)):

```bash
cp fixtures/jira-done.json fixtures/jira.json
bf collect jira
bf read
bf read jira:review
sleep 1
bf update --job watcher
bf read actions
```

Collection updates `jira:review`, Home flags the project with `newer_evidence`, and the watcher writes a second draft action (`actions` lists both drafts):

```json
{
  "ref": "projects/new-website.md",
  "review": true,
  "review_reasons": ["newer_evidence"],
  "new_links": 1,
  "newer": ["jira:review"],
  "next": "Run the keyboard navigation check."
}
```

Clean up the demo directory when finished:

```bash
cd
rm -rf -- "$context_demo"
```

## Use it on your work

Pick one cross-tool question, author its project note, and connect only the sources it needs: [GitHub history](../sensors/github-history.md) (`gh`), [Calendar](../sensors/google-calendar.py) / [Gmail](../sensors/gmail-headers.py) / [Drive](../sensors/google-drive-folders.py) (`gws`), Jira (`acli`), or Gcloud (`gcloud`), then add an assistant trigger (`on: { event: { kind: record, source: ... } }`) or [`records-gate.py`](../routines/README.md#react-to-new-records). See [Jobs](../../docs/docs/jobs.md) and [Assist](../../docs/docs/assist.md).
