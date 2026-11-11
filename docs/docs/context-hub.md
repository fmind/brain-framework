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
    { "ref": "gcloud:deployment", "fields": { "kind": "deployment", "status": "healthy" } },
    { "ref": "github:implementation", "fields": { "kind": "pull-request", "status": "merged" } },
    { "ref": "jira:review", "fields": { "kind": "issue", "status": "In review" } },
    { "ref": "workspace:brief", "fields": { "kind": "brief", "status": "approved" } }
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

Each tool names its project and status differently, and `bf.yaml` maps them once at collection ([field mappings](schema.md#shared-fields-and-sensor-mappings)):

| Tool      | Project pointer                 | Status pointer      | Mapped `fields`                           |
| --------- | ------------------------------- | ------------------- | ----------------------------------------- |
| Workspace | `attributes.project`            | `attributes.state`  | `project`, `status`, `kind: brief`        |
| Jira      | `attributes.workstream`         | `attributes.status` | `project`, `status`, `kind: issue`        |
| GitHub    | `attributes.repository_project` | `attributes.state`  | `project`, `status`, `kind: pull-request` |
| Gcloud    | `attributes.service_project`    | `attributes.health` | `project`, `status`, `kind: deployment`   |

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
fields:
  project:
    description: Project explicitly identified by the source.
    type: identity
    cardinality: one
    relation: true
sensors:
  jira:
    command: [sensors/demo.py, jira, "{{end}}"]
    mode: snapshot
    fields:
      project: { path: /attributes/workstream }
      kind: { value: issue }
      status: { path: /attributes/status }
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

An [assistant](prompts.md) schedule can filter changes with built-in declarative `when: { sources: [jira] }` or a custom [`gate`](prompts.md#gates) script ([records-gate.py](https://github.com/fmind/brain-framework/blob/main/examples/routines/records-gate.py), built on `_bf.py`, when you also filter fields with `--where`):

```bash
mkdir -p routines
cp "$context_demo/source/examples/routines/"*.py routines/
cat >> bf.yaml <<'EOF'
assistants:
  watcher:
    prompt: prompts/watcher.md
    command: [printf, '{"say":"Jira changed: check whether the launch decision still holds.","wake":null}']
    reply: { path: "" }
    schedules:
      changes: { every: 1, gate: { command: [python3, routines/records-gate.py, jira] } }
EOF
bf validate
bf update --assistant watcher
sleep 1
bf update --assistant watcher
```

`printf` stands in for an LLM harness and returns a [structured reply](prompts.md#structured-replies). Validation stays `"valid":true`; the first update finds `jira:review` and writes a `record` under `memories/watcher`, while the second finds nothing new and starts no harness (`"status":"ran"` with no `record`).

## Follow a change

Move the Jira fixture to `Done` (`attributes.updated` updates while event `time` stays fixed; [good records](sensors.md#good-records)):

```bash
cp fixtures/jira-done.json fixtures/jira.json
bf collect jira
bf read
bf read jira:review
sleep 1
bf update --assistant watcher
bf read memories/watcher/7d
```

Collection reports `"updated":1`, Home flags the project with `newer_evidence`, and the watcher runs once for the transition (`"total":2` in `memories/watcher/7d`):

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

Pick one cross-tool question, author its project note and connect only the sources it needs: [GitHub history](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md) (`gh`), [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) / [Gmail](https://github.com/fmind/brain-framework/blob/main/examples/sensors/gmail-headers.py) / [Drive](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) (`gws`), Jira (`acli`) or Gcloud (`gcloud`), then add an assistant schedule with `when:` or [`records-gate.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/README.md#react-to-new-records). See [sensors](sensors.md) and [team pilots](team.md#evaluate-a-pilot).
