# One context across four tools

A fictional Google Workspace brief, Jira review, GitHub pull request and Gcloud deployment describe the same New website project with different field names. Collect them into one brain, record whether the site can launch, then watch BF flag that conclusion when the Jira review moves to Done.

Follow the walkthrough in [Connect context across tools](../../docs/docs/context-hub.md). `tests/test_context_hub.py` runs its commands on a disposable copy of this folder.

| File                               | Purpose                                                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------ |
| [bf.yaml](bf.yaml)                 | Maps each tool's project and status fields to the shared `project` relation and `status`.  |
| [sensors/demo.py](sensors/demo.py) | Prints `fixtures/TOOL.json` as that tool's record; contacts nothing.                       |
| `fixtures/*.json`                  | Fictional tool output; `jira-done.json` is the review after the keyboard navigation check. |
| `projects/new-website.md`          | The minimal project note that the records link to through its `project:new-website` alias. |
| `evals/retrieval.yaml`             | Ten retrieval cases: `bf eval` reports `"score":"10/10"`.                                  |

The fixtures are adapter output, not provider API payloads, and the demo makes no claim about time saved or answer quality. A real sensor replaces `demo.py` with selected output from `gws`, `acli`, `gh` or `gcloud`; see the [sensor contract](../sensors/README.md).
