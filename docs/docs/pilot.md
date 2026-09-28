---
description: Evaluate whether shared context helps your team, with comparable tasks and explicit evidence.
---

# Evaluate a team pilot

Test whether a shared information hub helps teammates answer recurring questions and act across tools. Start with one project, three to five volunteers and four weeks. The [four-tool demo](context-hub.md) explains the mechanism; this pilot measures usefulness. No adoption, time saving or successful team outcome is assumed.

## Choose a question worth centralizing

Ask each participant about the last time they had to reconstruct context from several tools: which question, which sources, what work followed, and where they got stuck. Choose five recurring questions from that work. Examples include “What blocks this launch?”, “Which revision implements this decision?” and “What changed since our last review?”

Use a [private team brain](team.md) with only evidence all participants may read. Record which source owns each fact: for example, Workspace owns the brief, Jira owns task status, GitHub owns implementation history and Gcloud owns deployment observations. Project notes connect those facts to decisions; they should not become a second manually maintained issue tracker.

Assign one person to confirm each project's decisions and one person to maintain each selected sensor. Start with selected notes or exports when live collection is not yet needed. A useful question determines what to gather; tool availability alone does not.

## Run four weeks

| When                    | Work                                                                                                                                                         | Evidence to keep                                                                               |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Before the introduction | Pick the project, participants, five questions and source scope. Observe the current workflow on comparable tasks.                                           | Baseline time, answer correctness, sources consulted and existing maintenance effort.          |
| Week 1                  | Use the [terminal walkthrough](context-hub.md), then answer a real question from a fresh session. Watch one person follow the instructions without coaching. | Setup time including prerequisites, first useful answer, interventions and failure points.     |
| Weeks 2–3               | Use BF during ordinary work. Capture decisions in existing reviews; update notes after meaningful outcomes. Avoid repeated usage reminders.                  | Voluntary use, supported outcomes, failed questions, capture and support effort.               |
| Week 4                  | Compare against the baseline and discuss cases where BF was ignored or abandoned.                                                                            | Counts with denominators, benefits, upkeep, disagreements and a continue/change/stop decision. |

Check both halves of the loop. One teammate should recover evidence they did not write; another should record a verified outcome that a later session can find. An onboarding demonstration alone does not establish either habit.

## Compare fairly

Use three conditions on equivalent tasks: the team's usual tools; the same curated notes and collected records read directly by an editor or agent; and those exact files retrieved through BF. This separates the benefit of organizing information from the additional benefit of BF's retrieval, shared schema and relationships.

Rotate condition order and use matched questions so remembering an earlier answer does not decide the result. Hold the available evidence, freshness, agent/model and task scope constant where possible; record exceptions. A reviewer should check the answer and citations against the sources, including unresolved questions and conflicting or stale evidence. Record failures as failures, with elapsed time to abandonment.

Measure time to a **correct, supported answer**, required live source requests when observable, missed blockers and maintenance effort. Count setup, collection, mapping, note upkeep and maintainer support separately. BF command counts and GitHub stars do not establish value; reduced tool traffic is useful only when answers stay correct and sufficiently fresh. Do not claim savings from the fictional demo or exclude failed attempts to improve a median.

## Decide from behavior

For five volunteers, these are suggested decision rules to agree before the pilot, not industry benchmarks or statistically validated adoption thresholds:

| Question                  | Proposed evidence                                                                                                                  |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Can people start?         | At least 4/5 reach a correct cited answer without intervention after following the instructions; report total setup time.          |
| Do people return?         | At least 3/5 use BF for real work without reminders in both final weeks. Count all five enrolled participants, including dropouts. |
| Does centralization help? | At least three participants demonstrate a useful answer or action spanning two or more sources, with correct evidence.             |
| Can others maintain it?   | At least two teammates contribute a useful update independently, and someone else can retrieve it.                                 |
| Is the effort justified?  | Observed task benefits justify capture, collection, mapping, upkeep and support costs; preserve cases where they do not.           |

If setup blocks people, simplify the observed steps. If setup succeeds but nobody returns, revisit the question's frequency and value. If the same files accessed directly work just as well, improve the organizational practice and keep BF's role small. If only the maintainer can keep it useful, simplify capture and ownership before expanding.

## Keep a small private record

Use a short voluntary weekly note rather than adding telemetry. Existing `bf status` usage counts can corroborate local activity; they cannot tell whether an answer helped. Keep participant identities and work evidence private and share aggregates only with permission.

Copy this into the pilot's existing project note; leave unobserved values as `not measured`:

```markdown
## Pilot evidence

- Period and enrolled participants: not measured
- Project, recurring questions and permitted sources: not recorded
- BF revision, agent/model and baseline conditions: not recorded
- Independent first successes / enrolled: not measured
- Voluntary users in both final weeks / enrolled: not measured
- Correct answers / attempted tasks, by condition: not measured
- Task time and source requests, including failures: not measured
- Setup, collection, curation and support time: not measured
- Independent contributions and useful cross-source outcomes: not measured
- Failures, missing evidence and reasons for non-use: not recorded
- Decision and next test: pending
```

## Share what worked

After a useful pilot, prepare a permission-cleared case study: the original recurring problem, comparable conditions, participant and task counts, observed results, maintenance cost, failures and limits. Show one question, the returned refs, exact evidence and the resulting action in a short terminal demonstration with a text transcript. Use fictional replacements for private content and label the demonstration as reconstructed when applicable.

Invite a small second group outside the team to follow the walkthrough independently. Their experience tests assumptions hidden by familiarity with the maintainer. Keep the project open source: reusable examples, missed-question reproductions and onboarding fixes are useful contributions. See [Contributing](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md). Publish results only after observing them and obtaining permission for any shared work or quotations.
