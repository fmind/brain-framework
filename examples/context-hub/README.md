# One context across four tools

A fictional Google Workspace brief, Jira review, GitHub pull request and Gcloud deployment describe the same New website project using different field names. Collect them once, then ask whether the site is ready to launch through one brain. The small sensor emits fixed fixtures: it needs no accounts, credentials or network and is not a live provider integration.

Follow the [terminal walkthrough](../../docs/docs/context-hub.md) for installation, a disposable copy, expected results and an agent prompt. From this repository's root, an existing checkout environment can run the same example without a global installation:

```bash
context_demo=$(mktemp -d)
cp -R examples/context-hub "$context_demo/brain"
uv run bf collect workspace --brain "$context_demo/brain"
uv run bf collect jira --brain "$context_demo/brain"
uv run bf collect github --brain "$context_demo/brain"
uv run bf collect gcloud --brain "$context_demo/brain"
uv run bf read project:new-website --brain "$context_demo/brain"
uv run bf read workspace:brief --brain "$context_demo/brain"
uv run bf read jira:review --brain "$context_demo/brain"
uv run bf read github:implementation --brain "$context_demo/brain"
uv run bf read gcloud:deployment --brain "$context_demo/brain"
uv run bf validate --brain "$context_demo/brain"
uv run bf eval --brain "$context_demo/brain"
```

Each collection reports one record. The project read links all four records; exact reads retain their distinct source URLs and normalized `project` and `kind` fields. Validation reports `"valid":true`; evaluation reports `"score":"10/10"`. Search and read do not rerun the sensors.

Inspect [bf.yaml](bf.yaml): the brief's `attributes.project`, Jira's `attributes.workstream`, GitHub's `attributes.repository_project` and Gcloud's `attributes.service_project` map to the same `project` relationship. These fictional adapter outputs already contain the explicit identity `project:new-website`; they are not raw provider API payloads. BF applies and validates the mappings automatically at ingestion; it does not discover the ontology or guess that two names refer to the same project.

The [sensor](sensors/demo.py) demonstrates the record interface. A real adapter replaces the literal fixture with selected output from `gws`, `acli`, `gh` or `gcloud` and handles authentication, pagination, failures and stable identities. See [sensor development](../sensors/README.md). Refresh is manual here (`refresh: 0`); collecting these fixtures today says nothing about a real provider's freshness.

To compare with plain files, use the same collected `memories/` and project note directly in your editor or agent. The [pilot guide](../../docs/docs/pilot.md) defines a fair comparison; this demo makes no claim about user time saved, adoption or model-answer quality.
