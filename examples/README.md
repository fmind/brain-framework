# Examples

Start with [one context across four tools](context-hub/README.md): collect fictional evidence, record why a healthy preview still cannot launch, then see BF flag that conclusion when the review changes. All runnable brains use local fixtures; no provider account or model is needed.

From the checkout, run `uv sync --locked` first. Follow each README's disposable-copy instructions so collection and edits stay outside the checked-in fixtures. The walkthroughs need Linux or macOS and run `bf` with the checkout's Python 3.14; the team, hook and pre-commit examples also use Git. Scripts you copy into a brain need only Python 3.11 or later as `python3`.

| Your question                                 | Example                              | Observable result                                                                                                                     |
| --------------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| Can I answer a question split across tools?   | [Context hub](context-hub/README.md) | Four records share one project identity; `10/10` retrieval cases pass; a changed Jira review flags the project with `newer_evidence`. |
| Can I resume work and revisit a decision?     | [Example brain](brain/README.md)     | Retrieve evidence, write an answer, detect a policy change and transfer a draft procedure; `16/16` cases pass before edits.           |
| Does retrieval find the right evidence?       | [Retrieval](retrieval/README.md)     | `23/23` cases rank the right section first, collapse duplicate records and report absent evidence; `mrr` is 1.0.                      |
| Can an agent start with the relevant project? | [Hooks](hooks/README.md)             | A local Git remote identity selects the owning project and its next task; a prompt lists its matching note section.                   |
| Can I run my own checks and reviews?          | [Routines](routines/README.md)       | A weekly review creates a draft action; a pre-commit hook refuses a commit while `bf validate` finds a broken link.                   |
| Can I see updates and failures?               | [Watch](watch/README.md)             | Two sources succeed, one intentionally fails; native schedule files can be inspected without installation.                            |
| Can two people contribute through Git?        | [Team](team/README.md)               | Independent records and actions merge; incompatible edits conflict and fail validation.                                               |

[Sensors](sensors/README.md) holds six programs to copy and adapt: local Git history, GitHub history, documents, selected highlights, Google Calendar and Drive folders. Provider examples need the owner's configured CLI; their tests use fake providers. Start with the local-file walkthrough before selecting a live source.

The normal test suite checks these examples on disposable data, including adapter failures. Retrieval scores measure the supplied cases only. Fixture results do not establish live-provider freshness, agent-answer quality or time saved; the [team pilot](../docs/docs/team.md#evaluate-a-pilot) describes a real comparison.
