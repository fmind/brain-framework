---
name: bf-setup
description: Set up a new or existing Brain Framework brain around the user's recurring questions, verify agent access and connect the first useful sources. Use for onboarding; use bf-maintain for ongoing operations.
license: MIT
compatibility: Requires Brain Framework 13 (the bf command) on Linux or macOS.
metadata:
  version: "13.0.1"
---

# bf-setup

Help the user centralize context for a recurring question across tools. Start from the last time they reconstructed that context: which sources, what action followed, and what their current workaround costs. Reuse answers and authorization already given. Choose one project and a few real questions; identify the authoritative sources, audience and note owner. For a team introduction, use the [pilot guide](https://fmind.github.io/brain-framework/docs/pilot/) to compare independent use and maintenance effort. A sensor is useful only when those questions need its evidence.

## Establish the brain

1. Establish the intended directory and audience. Recommend `~/brain` for a new personal brain unless the user chose another path, such as `~/team-brain` or `~/brains/default`. Inspect an existing brain's `bf.yaml`, instructions and notes before changing it; preserve its name, customizations and unrelated work. Do not initialize over an existing brain. Keep personal evidence separate from shared work; a related brain expands retrieval scope.
1. Check `bf --version` against this skill's compatibility. If installation is needed, follow the release-matched [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/); do not silently upgrade an existing installation or rewrite its format. Use `bf init ~/brain`, substituting the chosen path. The path is required, and its final directory name becomes the brain name unless `--name NAME` is supplied. A path selects a brain without global registration.
1. Save one user-grounded OKF project note in `projects/`, with `type: project`, `status: draft|stable|deprecated`, its reason and next action. Follow [bf-learn](../bf-learn/SKILL.md) for authoring when available; the [brain layout](https://fmind.github.io/brain-framework/docs/brain/) owns field contracts. Do not invent decisions to populate the brain.
1. Run the new brain's starter `evals/retrieval.yaml`, then search for the saved decision's reason and read its ref to check it answers the question. Extend or replace the welcome-note checks with the user's real questions, expected refs, answer fragments and an unrelated empty query, following the [retrieval guide](https://fmind.github.io/brain-framework/docs/checks/#retrieval-cases). Preserve customized suites in an existing brain; add `evals/` if it predates the starter. Keep technical sensor/routine tests in `tests/`. Run `bf validate --brain PATH` and `bf eval --brain PATH`; inspect `problems` and `stale` before claiming success. Passing starter checks alone does not establish that the user's questions are answered.

Use explicit selection throughout, substituting the chosen path and actual query/ref:

```bash
bf read --brain PATH
bf search "reason for the decision" --brain PATH
bf read projects/PROJECT.md#decision --brain PATH
bf validate --brain PATH
bf eval --brain PATH
```

## Connect the agent

For a terminal agent, verify a first explicit CLI task before requiring skill installation. The [four-tool walkthrough](https://fmind.github.io/brain-framework/docs/context-hub/#give-a-terminal-agent-the-same-context) supplies a prompt and expected reads over fictional evidence. Then repeat with a real pilot question and inspect the host's tool history. Demo success does not establish work-account access or user value.

Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md) for the user's host. Install complete folders from a reviewed, matching release; preserve installed customizations. `bf-use` provides retrieval, `bf-learn` maintains notes, and `bf-action` tracks a session when requested. If the host uses MCP instead, follow the [MCP guide](https://fmind.github.io/brain-framework/docs/mcp/). Host setup is optional when the user wants a CLI-only brain.

Explain the relevant data boundary before connecting a cloud agent: its provider can receive the notes and records it reads, even though BF retrieval runs offline. Keep the selected audience explicit. Do not alter unrelated host settings.

Verify from a fresh host session: ask the agent a pilot question, have it search the intended brain, read the source and cite its ref. Copying a skill or registering a server proves configuration only. If you cannot observe that session, report host verification as pending and give the user the exact question to try.

## Connect useful sources

Offer [bf-scan](../bf-scan/SKILL.md) when the user wants help discovering sources; skip it when the useful sources are already known or discovery is declined. Obtain its scoped inspection approval before reading personal inventories. Discovery can also be run later on an established brain.

Choose the smallest useful set of candidates. Prefer `bf watch` for ongoing refresh and visible status, with optional `settings/watch.yaml` preferences; native scheduling remains opt-in. Hand sensor implementation, fake-provider tests, authorized live previews and scheduling to [bf-maintain](../bf-maintain/SKILL.md). Install that companion if needed; do not invent a second maintenance procedure. The [sensor guide](https://fmind.github.io/brain-framework/docs/sensors/) describes the contract when the companion is unavailable. A source found during discovery is not approval to authenticate, collect or schedule it. Add a deterministic routine only for a recurring review the user needs.

For each selected source, establish a question, account/folder/repository scope, fields to retain, freshness need and retrieval case. Configure common fields and explicit identities across tools: ingestion applies the mappings automatically, without inferring an ontology or merging similar names. Prepare and test disabled configuration before requesting any still-missing live execution authority. Reuse authority already supplied, and verify that collected evidence answers the question after an authorized run.

## Finish with evidence

For a pilot, retain the agreed questions, source owners, comparison conditions and next review in its existing project note. Leave unmeasured outcomes unknown. Check whether another person can recover the evidence and maintain a useful update; count setup, capture and support effort as well as task benefits. User outreach, identifiable feedback collection and publication need their own authority.

Report the brain location, questions answered with refs, validation/evaluation results and observed host access. Distinguish sources proposed, configured, successfully collected and scheduled with an observed run. Name pending access or unanswered questions and the next concrete step. A useful notes-only brain is a valid outcome; do not add integrations to fill a checklist. Keep reviewed setup decisions in the existing project note when authorized, without storing raw discovery inventories.

For the first collection, follow the [Getting started sensor and schema example](https://fmind.github.io/brain-framework/docs/getting-started/#collect-your-first-source) inside the selected brain. It uses a fictional local file, a manual sensor and one shared field. Verify the stored record, mapped field and source link before adding live providers.
