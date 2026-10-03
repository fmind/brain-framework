---
description: Run a fictional four-tool example that collects evidence, records a conclusion and flags it when a source changes.
---

# Connect context across tools

Ask one question across Google Workspace, Jira, GitHub and Gcloud: **“Can the New website launch, what remains, and why did we choose one page?”** This walkthrough collects each tool's evidence into one brain, records the answer in the project, then shows BF flagging that answer when Jira changes.

The four records are fictional fixtures: no provider account, credential, network access or model is needed. URLs under `example.test` are illustrative. Complete [Getting started](getting-started.md) first: this example combines sensors, field mappings and identities.

## Get the example

You need Linux or macOS, BF, Git and `python3`. Copy the example from the release matching your installation into a disposable folder:

```bash
context_demo=$(mktemp -d)
git clone --quiet --depth 1 --branch "v$(bf --version)" https://github.com/fmind/brain-framework.git "$context_demo/source"
cp -R "$context_demo/source/examples/context-hub" "$context_demo/brain"
cd "$context_demo/brain"
```

The brain holds a minimal project note, `bf.yaml` and a small sensor, `sensors/demo.py`, that prints `fixtures/TOOL.json` as if each tool had returned it. Review both before running anything. From a source checkout, copy `examples/context-hub` instead and run `uv run bf`.

## Collect four tools

```bash
bf collect workspace
bf collect jira
bf collect github
bf collect gcloud
bf read project:new-website
```

Each collection reports `"records":1`. `project:new-website` is the project note's alias, so the read opens the note. Its `backlinks` group the four records under the shared `project` relation, each with its `fields` side by side:

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

The previews already answer most of the question. Read the blocking record and check the brain:

```bash
bf read jira:review
bf validate
bf eval
```

The Jira review says launch is blocked until the keyboard navigation check passes. Validation reports `"valid":true`, and the example's retrieval cases report `"score":"10/10"`. The evidence supports a precise answer: **the implementation is merged and its preview is healthy, but accessibility review still blocks launch.** A healthy deployment alone cannot establish launch readiness. These reads reuse the saved records: no sensor runs again.

## How the four tools share one project

Each tool names its project and state differently. `bf.yaml` maps them into the same shared fields:

| Tool      | Project field                   | Status field        | Shared fields                             |
| --------- | ------------------------------- | ------------------- | ----------------------------------------- |
| Workspace | `attributes.project`            | `attributes.state`  | `project`, `status`, `kind: brief`        |
| Jira      | `attributes.workstream`         | `attributes.status` | `project`, `status`, `kind: issue`        |
| GitHub    | `attributes.repository_project` | `attributes.state`  | `project`, `status`, `kind: pull-request` |
| Gcloud    | `attributes.service_project`    | `attributes.health` | `project`, `status`, `kind: deployment`   |

For example, the Jira part of the configuration:

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

At collection, BF validates each mapped value, stores it under the record's `fields` and turns `project` into a relation to `project:new-website`. The mapping runs automatically; its meaning is configured by you. BF never guesses that similar names identify the same project. See [field mappings](schema.md#shared-fields-and-sensor-mappings).

## Record the conclusion

Write what the reads establish into the owning project, dated today:

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

The home page lists the project under `changed`, with its new `next` task. It is not flagged for review: no linked evidence is newer than the edit.

## Follow a change

In the fictional tracker, the keyboard navigation check passes and the review moves to Done. Swap in the tracker's next fixture and collect Jira again. Like a [good record](sensors.md#good-records), the fixture keeps the issue's event `time` and records the change in `attributes.updated`; its value `now` makes the demo sensor date the change at collection, as a tracker reports a transition that just happened:

```bash
cp fixtures/jira-done.json fixtures/jira.json
bf collect jira
bf read
bf read jira:review
```

Collection reports `"updated":1`. The home page now flags the project: the Jira review linked to it changed upstream after its last edit, and `newer` names the record to read:

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

The Jira record's `fields` now hold `"status":"Done"`. The saved conclusion is out of date: update the Launch review and Next actions, and the flag clears with the edit. This is the loop BF supports: gather, connect, act, learn, and notice when evidence moves on.

## Give a terminal agent the same context

Print the demo path with `echo "$context_demo/brain"`. In a fresh terminal-agent session, replace `ABSOLUTE_DEMO_PATH` in this prompt:

> Work in `ABSOLUTE_DEMO_PATH` and use the `bf` CLI. Read `project:new-website`, then the Workspace, Jira, GitHub and Gcloud records supporting it. Can the website launch, what remains, and why did we choose one page? Cite each supporting ref and separate recorded facts from inference. Report incomplete or outdated evidence. Do not collect, fetch source URLs or change files.

No skill or MCP setup is needed for this first task. Check the tool history: the agent should read the records and cite the Jira review before answering. A plausible answer without those reads does not verify access. Install [the skills](agents.md#install-the-skills) to make the procedure available in every session. A cloud agent's provider may receive what the agent reads.

Remove the disposable copy when you are done:

```bash
cd
rm -rf -- "$context_demo"
```

## Use it on your work

Choose one recurring question whose answer spans tools. Keep the decision in its project note and collect only the evidence the question needs:

| Tool             | Access and starting point                                                                                                                                                                                                                                                                    | What you implement                                                                             |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| GitHub           | [`gh`](https://cli.github.com/) and the reviewed [GitHub history sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/github-history.md) for commits, issues and pull requests.                                                                                       | The repositories to select.                                                                    |
| Google Workspace | [`gws`](https://github.com/googleworkspace/cli) and the reviewed [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) and [Drive folder](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) sensors. | Extraction of selected document content; the folder sensor lists folders, not document bodies. |
| Jira             | [`acli`](https://developer.atlassian.com/cloud/acli/) or the API.                                                                                                                                                                                                                            | A sensor for selected issues, their status and explicit project identities.                    |
| Gcloud           | [`gcloud`](https://cloud.google.com/sdk/gcloud) or the API.                                                                                                                                                                                                                                  | A sensor for selected deployment revisions and observed service state.                         |

A sensor is a script printing JSON records; provider tools own authentication. Your script owns scope, complete pagination, output bounds, failures and identity normalization. Test it with fake provider output before collecting, as the [sensor guide](sensors.md) describes. The [team pilot](team.md#evaluate-a-pilot) checks whether the loop helps people in practice.
