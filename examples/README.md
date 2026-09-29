# Examples

Start with [one context across four tools](context-hub/README.md): collect fictional evidence, explain why a healthy preview still cannot launch, and save the next decision with its sources. All runnable brains use local fixtures; no provider account or model is needed.

From the checkout, run `uv sync --locked` first. Follow each README's disposable-copy instructions so collection and edits stay outside the checked-in fixtures. The walkthroughs need Linux or macOS and run `bf` with the checkout's Python 3.14; the team and hook examples also use Git. Scripts you copy into a brain need only Python 3.11 or later as `python3`.

| Your question                                 | Example                              | Observable result                                                                                                           |
| --------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Can I answer a question split across tools?   | [Context hub](context-hub/README.md) | Four records share one project identity; the open review explains the launch blocker; `10/10` retrieval cases pass.         |
| Can I resume work and revisit a decision?     | [Example brain](brain/README.md)     | Retrieve evidence, write an answer, detect a policy change and transfer a draft procedure; `16/16` cases pass before edits. |
| Does retrieval find the right evidence?       | [Retrieval](retrieval/README.md)     | `17/17` cases rank the right project section first, collapse duplicate records and report absent evidence; `mrr` is 0.95.   |
| Can an agent start with the relevant project? | [Hooks](hooks/README.md)             | A local Git remote identity selects the owning project and its next task; a prompt lists its matching note section.         |
| Can I prepare a repeatable review?            | [Routines](routines/README.md)       | A deterministic program creates a draft action with open work and evidence refs.                                            |
| Can I see updates and failures?               | [Watch](watch/README.md)             | Two sources succeed, one intentionally fails; native schedule files can be inspected without installation.                  |
| Can two people contribute through Git?        | [Team](team/README.md)               | Independent records/actions merge; incompatible edits conflict and fail validation.                                         |

[Sensors](sensors/README.md) contains five adapters to copy and adapt: local Git, documents, selected highlights, Google Calendar and Drive folders. Google examples require the owner's configured `gws`; their tests use fake providers. Start with the linked local-file walkthrough before selecting a live source.

The normal test suite checks these examples on disposable data, including adapter failures. Retrieval scores measure the supplied cases only. Fixture results do not establish live-provider freshness, agent-answer quality or time saved; use the [pilot guide](../docs/docs/pilot.md) for a real comparison.
