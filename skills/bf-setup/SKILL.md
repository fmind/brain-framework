---
name: bf-setup
description: Set up a new or existing Brain Framework brain around recurring questions, verify retrieval and agent access, and hand selected integrations to bf-maintain. Use for first-use onboarding.
license: MIT
compatibility: Requires Brain Framework 14 (the bf command) on Linux or macOS.
metadata:
  version: "14.0.0"
---

# bf-setup

Start with one project and a few questions the user repeatedly reconstructs across tools. Reuse the supplied directory, audience, authoritative sources and authorization. Identify the note owner and what useful first answer would look like; a notes-only brain is a valid outcome.

## Establish the brain

1. Inspect an existing brain's `bf.yaml`, instructions and relevant notes; preserve its name, customizations and unrelated work. For a new personal brain, default to `~/brain` unless the user chose a path. Do not initialize over an existing brain.
1. Check `bf --version` against this skill's compatibility. Install only when needed using the [getting-started guide](https://fmind.github.io/brain-framework/docs/getting-started/); never change an existing installation silently. For a new brain, run `bf init ~/brain`, substituting the chosen path. Its name derives from the final directory name, lowercased and hyphenated to fit `[a-z][a-z0-9-]{0,63}`, unless `--name NAME` is supplied; use the `brain` field that `bf init` returns in `bf://` addresses. Registration is optional: it lets search and read select the brain by name, never runs its programs.
1. Save one user-grounded project note with `type: project`, `status: draft|stable|deprecated`, the decision under `## Decision {#decision}`, its reason and a next task. Use `bf-learn` when installed or the [brain layout](https://fmind.github.io/brain-framework/docs/brain/). Do not invent a decision to fill the example.
1. Run the starter evaluation for a new brain; expect `"score":"3/3"`. Add real questions with expected refs and answer fragments, plus an unrelated empty query, in a suite starting with `version: 5`, following the [retrieval cases](https://fmind.github.io/brain-framework/docs/checks/#retrieval-cases). Preserve existing suites; add `evals/` without reinitializing if needed.

Inside the chosen directory, with no conflicting `BF_BRAIN`, run:

```bash
bf read
bf search "reason for the decision"
bf read 'projects/PROJECT.md#decision'
bf validate
bf eval
```

Replace the query and ref with the saved note. Outside the directory, pass `--brain PATH`. Expect the actual reason at its ref, `"valid":true` and passing retrieval cases; inspect `problems` and `stale`. Starter checks alone do not establish that the user's questions are answered. The [first-decision walkthrough](https://fmind.github.io/brain-framework/docs/getting-started/#save-a-decision) provides runnable fictional input and expected output.

## Verify agent access

For a terminal agent, try an explicit CLI search/read task before requiring skill installation. Install complete, reviewed folders using the [installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md); preserve host settings and installed customizations. If the host uses MCP, follow the [MCP guide](https://fmind.github.io/brain-framework/docs/mcp/). Host setup is optional for CLI-only use.

Explain the data boundary before connecting a cloud agent: its provider may receive returned notes and records even though BF retrieval is offline. Personal and shared brains have different audiences; adding a related brain expands retrieval scope.

From a fresh host session, ask a pilot question, search the intended brain, read the source and cite its ref. Inspect the tool history. Copying a skill or registering a server proves configuration only; if the session cannot be observed, report verification pending with the exact question to try. The [four-tool walkthrough](https://fmind.github.io/brain-framework/docs/context-hub/#give-a-terminal-agent-the-same-context) offers a fictional rehearsal.

## Add sources only when useful

Use `bf-scan` for requested discovery; skip scanning when sources are already known. For each selected source, retain the question, account/folder/repository scope, fields, exclusions, freshness requirement and retrieval case. Discovery is not authority to authenticate, collect or schedule.

Use `bf-maintain` for disabled configuration, explicit schema mappings, fake-provider tests and authorized live runs. Install that companion when needed; the [sensor guide](https://fmind.github.io/brain-framework/docs/sensors/) owns the contract. The [local-file sensor example](https://fmind.github.io/brain-framework/docs/getting-started/#collect-your-first-source) checks the stored record, mapped field and source link without a live provider. Prefer `bf watch` for authorized ongoing refresh; native scheduling is opt-in. Reuse existing execution authority and request only what is missing after preparing a testable integration.

## Finish with evidence

Report the brain location, questions answered with refs, validation/evaluation results and observed host access. Distinguish proposed, configured, collected and scheduled sources with observed runs; name unresolved access or evidence gaps. Retain reviewed setup decisions in the owning project, without raw discovery inventories.

For a team pilot, use the [pilot guide](https://fmind.github.io/brain-framework/docs/pilot/) to record comparison conditions, source owners, setup/support effort and a next review. Leave unmeasured outcomes unknown; outreach, identifiable feedback collection and publication need their own authority.
