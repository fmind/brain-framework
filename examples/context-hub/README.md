# One context across four tools

A fictional Google Workspace brief, Jira review, GitHub pull request and Gcloud deployment describe the same New website project using different field names. Collect them once, then ask whether the site is ready to launch through one brain. The small sensor emits fixed fixtures: it needs no accounts, credentials or network and is not a live provider integration.

Follow the [terminal walkthrough](../../docs/docs/context-hub.md) for installation and an agent prompt. From this repository's root after `uv sync --locked`, run the following complete local exercise. The subshell isolates configuration and state and removes its temporary files on exit:

```bash
(
  set -eu
  bf_checkout=$PWD
  context_demo=$(mktemp -d)
  context_demo=$(cd "$context_demo" && pwd -P)
  trap 'rm -rf -- "$context_demo"' EXIT
  cp -R examples/context-hub "$context_demo/brain"
  cd "$context_demo/brain"
  bf() {
    env -u BF_BRAIN XDG_CONFIG_HOME="$context_demo/config" XDG_STATE_HOME="$context_demo/state" \
      uv run --project "$bf_checkout" bf "$@"
  }
  bf collect workspace
  bf collect jira
  bf collect github
  bf collect gcloud
  bf read project:new-website
  bf read workspace:brief
  bf read jira:review
  bf read github:implementation
  bf read gcloud:deployment
  bf validate
  bf eval

  # Record the conclusion from those four reads in the owning project.
  cat >> projects/new-website.md <<'EOF'

## Launch review

Hold public launch until the keyboard navigation check passes. The [brief](workspace:brief) explains the single-page choice; the [merged implementation](github:implementation) and [healthy preview](gcloud:deployment) establish progress. The [open review](jira:review) still blocks launch. Next: perform the check, retain its result and refresh the issue evidence. No successful check is recorded yet.
EOF
  bf search "Hold public launch" --scope projects
  bf read projects/new-website.md#launch-review
  bf validate
)
```

Each collection reports one record. The project read links all four records; exact reads retain their distinct source URLs and normalized `project` and `kind` fields. Validation reports `"valid":true`; evaluation reports `"score":"10/10"`. Search and read do not rerun the sensors.

The final search recovers `projects/new-website.md#launch-review` with the decision to hold launch. Its exact read keeps the rationale, supporting refs and next action together. This is the act-and-learn step: a person or agent writes a conclusion after inspecting evidence; BF makes it available to the next session. Perform the actual check before recording a successful outcome.

Inspect [bf.yaml](bf.yaml): the brief's `attributes.project`, Jira's `attributes.workstream`, GitHub's `attributes.repository_project` and Gcloud's `attributes.service_project` map to the same `project` relationship. These fictional adapter outputs already contain the explicit identity `project:new-website`; they are not raw provider API payloads. BF applies and validates the mappings automatically at ingestion; it does not discover the ontology or guess that two names refer to the same project.

The [sensor](sensors/demo.py) demonstrates the record interface. A real adapter replaces the literal fixture with selected output from `gws`, `acli`, `gh` or `gcloud` and handles authentication, pagination, failures and stable identities. See [sensor development](../sensors/README.md). Refresh is manual here (`refresh: 0`); collecting these fixtures today says nothing about a real provider's freshness.

To compare with plain files, use the same collected `memories/` and project note directly in your editor or agent. The [pilot guide](../../docs/docs/pilot.md) defines a fair comparison; this demo makes no claim about user time saved, adoption or model-answer quality.
