# Connect context across tools

Ask one question across Google Workspace, Jira, GitHub and Gcloud: **“Is the New website ready to launch, what remains, and why did we choose one page?”** Collect their selected evidence once, map it into a shared schema, then let your terminal agent read it together.

This walkthrough uses four fictional records. It needs no provider accounts, credentials or model calls. The tiny adapter emits fixtures rather than contacting those services; source URLs under `example.test` are illustrative and must not be fetched.

**Advanced:** complete [Getting started](getting-started.md) first. This combines sensors, schema mappings, identities and agent retrieval.

## Run the example

You need Linux or macOS, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git and `python3` for the standard-library demo sensor. Install BF if needed:

```bash
uv tool install brain-framework
bf --version
```

If `bf` is not on PATH, run `uv tool update-shell` and open a new shell. From a source checkout, you can instead use `uv run bf` for each command; the [example README](https://github.com/fmind/brain-framework/tree/main/examples/context-hub) shows that route.

Create a disposable directory, retrieve the example and copy it into its own brain:

```bash
context_demo=$(mktemp -d)
git clone --depth 1 https://github.com/fmind/brain-framework.git "$context_demo/source"
cp -R "$context_demo/source/examples/context-hub" "$context_demo/brain"
cd "$context_demo/brain"
```

Review `bf.yaml` and `sensors/demo.py` in that copy. All four collection commands run the reviewed local fixture script:

```bash
bf collect workspace
bf collect jira
bf collect github
bf collect gcloud
bf read project:new-website
```

Each collection returns `"records":1`. The project read returns the project note with `backlinks` under the shared `project` relationship, including these four refs:

| Ref                     | Saved evidence                                                |
| ----------------------- | ------------------------------------------------------------- |
| `workspace:brief`       | Visitors need a clear explanation before signing up.          |
| `jira:review`           | Launch is blocked until the keyboard navigation check passes. |
| `github:implementation` | Revision `demo-42` was merged.                                |
| `gcloud:deployment`     | The same revision has a healthy preview deployment.           |

Read the originals and verify the brain:

```bash
bf read workspace:brief
bf read jira:review
bf read github:implementation
bf read gcloud:deployment
bf validate
bf eval --path evals/retrieval.yaml
```

Validation reports `"valid":true`; evaluation reports `"score":"10/10"`. The evidence supports a precise answer: **the implementation reached preview, but launch remains blocked by accessibility review; the next action is the keyboard navigation check.** The Workspace brief explains the single-page choice. A healthy deployment alone cannot establish launch readiness.

Repeat those reads, or search `"keyboard navigation"` with `--scope memories/jira`: retrieval reuses the saved context without executing the four sensors again. This proves the offline path on these fixtures, not a measured saving in provider requests for a real team. A later provider change needs explicit collection before the saved evidence reflects it.

## See the automatic normalization

The four adapter outputs name their project field differently. `bf.yaml` declares how each maps into the same ontology:

| Adapter output field                    | Shared field | Explicit identity     |
| --------------------------------------- | ------------ | --------------------- |
| Workspace: `attributes.project`         | `project`    | `project:new-website` |
| Jira: `attributes.workstream`           | `project`    | `project:new-website` |
| GitHub: `attributes.repository_project` | `project`    | `project:new-website` |
| Gcloud: `attributes.service_project`    | `project`    | `project:new-website` |

For example, this part of the configuration maps the Jira adapter:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
schema:
  project:
    description: Project explicitly identified by the source.
    type: identity
    cardinality: one
    relation: true
sensors:
  jira:
    command: [sensors/demo.py, jira]
    fields:
      project: { path: /attributes/workstream }
```

On collection, BF validates the value, saves `fields.project: project:new-website` and creates a `project` relationship supported by that record. The example also maps a common `kind` field. Original attributes, source URLs and record refs remain available.

**The mappings run automatically; their meaning is explicitly configured.** These fixtures are adapter outputs, not provider API schemas. A real sensor normalizes known provider identities, or a reviewed mapping supplies the common identity for its selected scope. BF never guesses that matching names identify the same project. See [schema mappings](schema.md#shared-fields-and-sensor-mappings).

## Give a terminal agent the same context

Print the absolute demo path with `echo "$context_demo/brain"`. In a fresh terminal-agent session, replace `ABSOLUTE_DEMO_PATH` in this prompt:

> Work in the demo brain directory and use the `bf` CLI. Read `project:new-website`, then read the Workspace, Jira, GitHub and Gcloud record refs supporting it. Is the website ready to launch, what remains, and why did we choose one page? Cite each supporting ref and distinguish recorded facts from inference. Report incomplete or stale evidence. Do not collect, fetch source URLs or change files.

No skill installation or MCP configuration is needed for this first explicit CLI task. The host must be able to execute `bf` and access the directory. Inspect its tool history: it should read all four records and cite the open Jira review before explaining the next action. A plausible answer without those reads does not verify access. Install [bf-use](agents.md#install-the-skills) later to make the retrieval procedure discoverable across sessions.

A cloud agent's provider may receive the evidence it reads. Keep the intended brain and audience explicit when moving from this fictional demo to work data.

## Use it on your work

Choose one recurring question whose answer is split across tools. Create a brain with [Getting started](getting-started.md), keep the decision in its owning project note, and connect only the evidence needed for that question:

| Tool             | Access and starting point                                                                                                                                                                                                                                                             | What you implement                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| GitHub           | [`gh`](https://cli.github.com/); the [Git sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/git-history.py) already handles local commit history.                                                                                                           | A sensor for selected issues or pull requests.                                                      |
| Google Workspace | [`gws`](https://github.com/googleworkspace/cli); reviewed [Calendar](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-calendar.py) and [Drive folder](https://github.com/fmind/brain-framework/blob/main/examples/sensors/google-drive-folders.py) sensors. | Extraction of selected document content; the folder sensor collects a catalog, not document bodies. |
| Jira             | [`acli`](https://developer.atlassian.com/cloud/acli/) or the API.                                                                                                                                                                                                                     | A sensor for selected issues, status and explicit project identities.                               |
| Gcloud           | [`gcloud`](https://cloud.google.com/sdk/gcloud) or the API.                                                                                                                                                                                                                           | A sensor for selected deployment revisions and observed service state.                              |

A sensor is a script that prints JSON records; any accessible CLI, API or file can be an input. Provider tools own authentication. Your adapter owns scope, complete pagination, output bounds, failures and identity normalization; BF validates, maps and retains the records. Test it with fake provider output before authorized collection. Follow [sensor development](https://github.com/fmind/brain-framework/tree/main/examples/sensors) and keep freshness requirements explicit.

Agents use that context to reason and perform authorized work through the original tools. BF does not execute provider actions through retrieval. After work, update the project with the verified outcome and next step, link its evidence, and refresh only the sources that need it. The [team pilot](pilot.md) checks whether this loop helps people in practice.
