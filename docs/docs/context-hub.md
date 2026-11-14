---
icon: lucide/network
description: Run a fictional four-tool example that collects evidence, records a conclusion and flags it when a source changes.
---

# Four-tool walkthrough

Answer one question across Google Workspace, Jira, GitHub and Gcloud fixtures: **"Can the New website launch, what remains, and why did we choose one page?"** Only the initial `git clone` uses the network; no credentials or LLM are required.

## Get the example

```bash
context_demo=$(mktemp -d)
git clone --quiet --depth 1 --branch "v$(bf --version)" https://github.com/fmind/brain-framework.git "$context_demo/source"
cp -R "$context_demo/source/examples/context-hub" "$context_demo/brain"
cd "$context_demo/brain"
```

The copy contains `projects/new-website.md`, `bf.yaml` and `sensors/demo.py` (which emits `fixtures/TOOL.json`). Review `bf.yaml` and `sensors/demo.py` before running.

## Collect four tools

```bash
bf collect workspace
bf collect jira
bf collect github
bf collect gcloud
bf read project:new-website
```

Each collection reports `"records":1`. Reading `project:new-website` opens the project note and groups all four records under the `project` backlink relation:

```json
{
  "relation": "project",
  "total": 4,
  "items": [
    { "ref": "gcloud:deployment", "@type": "Deployment" },
    { "ref": "github:implementation", "@type": "PullRequest" },
    { "ref": "jira:review", "@type": "Issue" },
    { "ref": "workspace:brief", "@type": "Brief" }
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

Each tool names its project and status differently, and `sensors/demo.py` normalizes them into `@type`, `relations.project` and `attributes`, while `bf.yaml` declares optional ontology constraints ([ontology rules](schema.md#shared-fields-and-sensor-mappings)):

| Tool      | Project pointer                 | Status pointer      | Normalized `@type` & `attributes`   |
| --------- | ------------------------------- | ------------------- | ----------------------------------- |
| Workspace | `attributes.project`            | `attributes.state`  | `Brief`, `kind: brief`, `status`    |
| Jira      | `attributes.workstream`         | `attributes.status` | `Issue`, `kind: issue`, `status`    |
| GitHub    | `attributes.repository_project` | `attributes.state`  | `PullRequest`, `kind: pull-request` |
| Gcloud    | `attributes.service_project`    | `attributes.health` | `Deployment`, `kind: deployment`    |

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
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

Home lists the project with its `next` task and no `review` flag (`changed` is empty because projects report under `projects`).

## Hear of each change once

A [scheduled job](prompts.md) can also react to record updates with `on: { event: { kind: [record.created, record.updated], source: jira } }` or a custom [`gate`](prompts.md#gates) script ([records-gate.py](https://github.com/fmind/brain-framework/blob/main/examples/routines/records-gate.py) when you also filter fields with `--where`):

```bash
mkdir -p routines
cp "$context_demo/source/examples/routines/records-gate.py" routines/
cat >> bf.yaml <<'EOF'
  watcher:
    prompt: prompts/watcher.md
    harness: [printf, "%s\n", "---\ntype: action\nstatus: draft\n---\n\n# Jira review\n\nJira changed: check whether the launch decision still holds.\n"]
    emit: action
    on: { every: 1 }
    gate: [python3, routines/records-gate.py, jira, --end, "{{end}}", --state, "{{state}}"]
EOF
bf validate
bf update --job watcher
sleep 1
bf update --job watcher
```

`printf` stands in for an LLM harness and prints a draft action note; `emit: action` writes `actions/YYYY-MM-DD_watcher.../ACTION.md`. Validation stays `"valid":true`; the first update finds `jira:review` and writes `"action"`, while the second finds nothing new and returns `"status":"gated"` without starting the harness.

## Follow a change

Move the Jira fixture to `Done` (`attributes.updated` updates while event `time` stays fixed; [good records](sensors.md#good-records)):

```bash
cp fixtures/jira-done.json fixtures/jira.json
bf collect jira
bf read
bf read jira:review
sleep 1
bf update --job watcher
bf read actions
```

Collection reports `"updated":1`, Home flags the project with `newer_evidence`, and the watcher emits a second draft action (`"total":2` in `bf read actions`):

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

Pick one cross-tool question, author its project note and connect only the sources it needs: [GitHub history](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md) (`gh`), [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) / [Gmail](https://github.com/fmind/brain-framework/blob/main/examples/sensors/gmail-headers.py) / [Drive](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) (`gws`), Jira (`acli`) or Gcloud (`gcloud`), then add a reactive job with `on: { event: ... }` or [`records-gate.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/README.md#react-to-new-records). See [sensors](sensors.md).
