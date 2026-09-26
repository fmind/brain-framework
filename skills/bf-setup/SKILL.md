---
name: bf-setup
description: Set up a new or existing Brain Framework brain around the user's recurring questions, verify agent access and connect the first useful sources. Use for onboarding; use bf-maintain for ongoing operations.
license: MIT
compatibility: Requires Brain Framework 13 (the bf command) on Linux or macOS.
metadata:
  version: "13.0.0"
---

# bf-setup

Help the user reach a useful answer with evidence. Start from their work: what do they repeatedly need to remember, find or explain, and is this a personal or team brain? Reuse answers and authorization already given. Choose a small pilot: one project and a few real questions. A sensor is useful only when those questions need evidence the notes do not contain.

## Establish the brain

1. Confirm the intended directory and audience. Inspect an existing brain's `bf.yaml`, instructions and notes before changing it; preserve its name, customizations and unrelated work. Do not initialize over an existing brain. Keep personal evidence separate from shared work; a related brain expands retrieval scope.
1. Check `bf --version` against this skill's compatibility. If installation is needed, follow the release-matched [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/); do not silently upgrade an existing installation or rewrite its format. Use `bf init PATH` for a new brain, without `--collect`. A path selects a brain without global registration.
1. Save one user-grounded decision or project note in `projects/`, with its reason and next action. Follow [bf-learn](../bf-learn/SKILL.md) for authoring when available; the [brain layout](https://fmind.github.io/brain-framework/docs/brain/) owns field contracts. Do not invent decisions to populate the brain.
1. Search for the reason, read the returned ref and check it answers the question. Add retrieval cases in `evals/` following the [retrieval guide](https://fmind.github.io/brain-framework/docs/search/#retrieval-cases), including an expected answer and an unrelated empty query. Preserve existing suites. Run `bf validate --brain PATH` and `bf eval --brain PATH`; inspect `problems` and `stale` before claiming success.

Use explicit selection throughout, substituting the chosen path and actual query/ref:

```bash
bf read --brain PATH
bf search "reason for the decision" --brain PATH
bf read projects/PROJECT.md#decision --brain PATH
bf validate --brain PATH
bf eval --brain PATH
```

## Connect the agent

Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md) for the user's host. Install complete folders from a reviewed, matching release; preserve installed customizations. `bf-use` provides retrieval, `bf-learn` maintains notes, and `bf-action` tracks a session when requested. If the host uses MCP instead, follow the [MCP guide](https://fmind.github.io/brain-framework/docs/mcp/). Host setup is optional when the user wants a CLI-only brain.

Explain the relevant data boundary before connecting a cloud agent: its provider can receive the notes and records it reads, even though BF retrieval runs offline. Keep the selected audience explicit. Do not alter unrelated host settings.

Verify from a fresh host session: ask the agent a pilot question, have it search the intended brain, read the source and cite its ref. Copying a skill or registering a server proves configuration only. If you cannot observe that session, report host verification as pending and give the user the exact question to try.

## Connect useful sources

Offer [bf-scan](../bf-scan/SKILL.md) when the user wants help discovering sources; skip it when the useful sources are already known or discovery is declined. Obtain its scoped inspection approval before reading personal inventories. Discovery can also be run later on an established brain.

Choose the smallest useful set of candidates. Hand sensor implementation, fake-provider tests, explicit execution trust, authorized live previews and scheduling to [bf-maintain](../bf-maintain/SKILL.md). Install that companion if needed; do not invent a second maintenance procedure. The [sensor guide](https://fmind.github.io/brain-framework/docs/sensors/) describes the contract when the companion is unavailable. A source found during discovery is not approval to authenticate, collect or schedule it. Add a deterministic routine only for a recurring review the user needs.

For each selected source, establish a question, account/folder/repository scope, fields to retain, freshness need and retrieval case. Prepare and test disabled configuration before requesting any still-missing live execution authority. Reuse authority already supplied, and verify that collected evidence answers the question after an authorized run.

## Finish with evidence

Report the brain location, questions answered with refs, validation/evaluation results and observed host access. Distinguish sources proposed, configured, successfully collected and scheduled with an observed run. Name pending access or unanswered questions and the next concrete step. A useful notes-only brain is a valid outcome; do not add integrations to fill a checklist. Keep reviewed setup decisions in the existing project note when authorized, without storing raw discovery inventories.
