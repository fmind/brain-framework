# Brain Framework skills

Markdown packages that teach agents to use Brain Framework. They are distributed from this repository, separately from the Python package.

| Skill                               | Example request                                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------------------------------ |
| [bf-setup](bf-setup/SKILL.md)       | “Set up `~/brain` and verify that you can find our project decision.”                            |
| [bf-scan](bf-scan/SKILL.md)         | “Suggest useful sources; propose an inspection scope first.”                                     |
| [bf-import](bf-import/SKILL.md)     | Read a selected source and its documentation; incorporate context, links and selected knowledge. |
| [bf-use](bf-use/SKILL.md)           | “Why did we choose a single product page? Read and cite the source.”                             |
| [bf-learn](bf-learn/SKILL.md)       | “Save this decision and its reason in the owning project.”                                       |
| [bf-action](bf-action/SKILL.md)     | “Resume the website review from its last verified state.”                                        |
| [bf-maintain](bf-maintain/SKILL.md) | “Diagnose why the local-documents source is stale.”                                              |

## Install

For a first terminal-agent task, use the explicit prompt in the [four-tool walkthrough](../docs/docs/context-hub.md#give-a-terminal-agent-the-same-context). The agent needs CLI and file access; a skill installation is optional for that task. Install skills when you want these procedures discovered across sessions.

Start with `bf-use`. Add `bf-setup` for onboarding, `bf-learn` for note updates or another skill when its task is needed. The package and `bf init` do not install skills.

Copy complete folders from a reviewed source archive matching `bf --version`. For a host that discovers `~/.agents/skills/`, run this from that archive for a first installation, when the destination does not exist:

```bash
mkdir -p ~/.agents/skills
cp -R skills/bf-use ~/.agents/skills/bf-use
```

Keep each folder's `references/`, `scripts/` and templates. `metadata.version` records the source release; compare it with `bf --version` after upgrading. For an existing installation, review and merge changes instead of overwriting customizations.

Host discovery paths differ. A brain's `skills/` can hold versioned workflow packages, but placing files there alone does not make a host load them. Use the host's configured discovery directory or a host-supported link, then start a new session and confirm `bf-use` is available.

Start a fresh host session after installation. With the [first decision](../docs/docs/getting-started.md) saved, ask:

> Use `bf-use` with `~/brain`. Find why we chose a single product page, read the source and cite its ref.

The agent should read `projects/new-website.md#decision` and report the saved reason: visitors need a clear explanation before signing up. A skill appearing in the host proves discovery; this search and read checks access to the intended brain. See the [agent walkthrough](../docs/docs/agents.md) or [MCP alternative](../docs/docs/mcp.md).

For development of Brain Framework itself, use the repository-local [bf-contribute](../.agents/skills/bf-contribute/SKILL.md) skill.

## Setup and source discovery

Ask `bf-setup`: "Help me set up a brain for this project and verify that my agent can explain our decisions." It establishes a small pilot, saves useful knowledge and verifies retrieval and agent access before adding integrations. It also works with an existing brain and preserves its configuration and notes.

Ask `bf-scan`: "Help me find useful sources; propose an inspection scope before reading my bookmarks or project folders." Agree on specific inputs and what the agent may receive. It distinguishes observed tools or links from unverified account access, compares existing coverage and recommends connections for recurring questions. Its optional standard-library helper supports Python 3.11+, selected Chromium JSON or Netscape HTML bookmark exports, and named executable availability checks. It returns bounded host counts or availability, never full bookmark URLs or executable paths; hostnames can still be sensitive.

Discovery does not enable sensors, authenticate accounts or save an inventory. Selected integrations pass to `bf-maintain` for disabled configuration, fake-provider tests and authorized live execution. Install companions as needed; the skills are independent folders, and sibling links do not install or activate another skill. Neither `bf-setup` nor `bf-scan` is a new `bf` subcommand.

## Import a selected source

Ask `bf-import`: "Read this project's handbook and its documentation, then add an overview and links to my brain so an agent can fetch current details later." Install it alongside `bf-use` when incorporating an identified source. It reads relevant content before choosing an overview with canonical links, a durable synthesis or selected evidence retained for a specific need. Knowledge changes, so a full copy is not the default. It preserves inspection limits and access requirements, then validates the note and checks retrieval. Recurring collection belongs to `bf-maintain`; `bf-import` is a skill, not a CLI subcommand.

## Decision workflows

Choose the guide that matches the next task:

| Task                     | Guide                                               | Result to retain                                      |
| ------------------------ | --------------------------------------------------- | ----------------------------------------------------- |
| Resume a session         | [Working context](bf-action/references/context.md)  | Last verified state, constraints and next step.       |
| Review a choice          | [Decisions](bf-action/references/decisions.md)      | Expected versus observed outcome, including unknowns. |
| Review current knowledge | [Periodic review](bf-learn/references/review.md)    | Updated project conclusions and unresolved questions. |
| Keep a source revision   | [Evidence](bf-learn/references/evidence.md)         | A selected exact-read capture and its original ref.   |
| Reuse a lesson           | [Consolidation](bf-learn/references/consolidate.md) | A draft procedure with evidence and limits.           |
| Share selected knowledge | [Sharing](bf-learn/references/share.md)             | Reviewed notes suitable for the destination audience. |

Load guides only when needed. Reviews, evidence capture and sharing do not require an action folder. Use `bf-action` when the user asks to track a session. The [example brain](../examples/brain/README.md#review-a-decision) demonstrates these workflows with fictional evidence.
