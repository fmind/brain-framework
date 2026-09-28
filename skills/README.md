# Brain Framework skills

Agent procedures for **gather → normalize → organize and connect → act → learn**. Skills are copied from this repository separately from the Python package; their names are not `bf` subcommands.

## Choose a skill

| Skill                               | Use it for                                    | Example request                                                         |
| ----------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------- |
| [bf-setup](bf-setup/SKILL.md)       | First-use onboarding                          | “Set up `~/brain` and verify that you can find our project decision.”   |
| [bf-scan](bf-scan/SKILL.md)         | Scoped source discovery                       | “Suggest useful sources; propose an inspection scope first.”            |
| [bf-import](bf-import/SKILL.md)     | Incorporating an identified source            | “Read this handbook and add an overview with links to current details.” |
| [bf-use](bf-use/SKILL.md)           | Finding and citing saved evidence             | “Why did we choose a single product page? Read and cite the source.”    |
| [bf-learn](bf-learn/SKILL.md)       | Updating or reviewing knowledge               | “Save this decision and its reason in the owning project.”              |
| [bf-action](bf-action/SKILL.md)     | Tracking an explicitly requested work session | “Resume the website review from its last verified state.”               |
| [bf-maintain](bf-maintain/SKILL.md) | Integration implementation and operations     | “Diagnose why the local-documents source is stale.”                     |

Start with `bf-use`, then add skills for the work you need. Setup and discovery hand selected integrations to maintenance. A source scan produces recommendations; it does not authenticate, collect or schedule. An import usually preserves useful context and pointers rather than a full copy. Ordinary retrieval, note updates and reviews need no action folder.

## Install and verify

For a first terminal-agent task, try the explicit prompt in the [four-tool walkthrough](../docs/docs/context-hub.md#give-a-terminal-agent-the-same-context); skill installation is optional. The package and `bf init` do not install skills.

Copy complete folders from a reviewed source matching `bf --version`, such as its release tag; the GitHub tag archive holds the same files. For a host that discovers `~/.agents/skills/`, this copies `bf-use` **only when the destination does not exist**:

```bash
skills_source=$(mktemp -d)
git clone --depth 1 --branch "v$(bf --version)" https://github.com/fmind/brain-framework.git "$skills_source"
mkdir -p ~/.agents/skills
[ -e ~/.agents/skills/bf-use ] || cp -R "$skills_source/skills/bf-use" ~/.agents/skills/bf-use
rm -rf -- "$skills_source"
```

Confirm that the copied `SKILL.md` has a `metadata.version` equal to `bf --version`.

Keep bundled references, scripts and templates. `compatibility` states runtime requirements; `metadata.version` identifies the source release. Local edits under `Unreleased` can differ from that release. Skill links open the current documentation site and the repository's `main` branch, which can describe a newer release than your copy; `bf --version`, `bf COMMAND --help` and the skill's `metadata.version` are authoritative for an installation. For an existing installation, review and merge changes without overwriting customizations. Python helpers require only the standard library and Python 3.11+; the BF package has its own interpreter requirement.

Host discovery paths differ. A brain's own `skills/` can hold versioned procedures but is not automatically a discovery directory. Use the host's configured location or supported link. Companion names and sibling links do not install another skill; add companions only when needed.

Start a fresh host session and confirm `bf-use` is available. With the [first decision](../docs/docs/getting-started.md#save-a-decision) saved, ask:

> Use `bf-use` with `~/brain`. Find why we chose a single product page, read the source and cite its ref.

Expect `projects/new-website.md#decision` and the saved reason: visitors need a clear explanation before signing up. Discovery confirms the host sees the skill; the search and exact read confirm access to the intended brain. Report an unobserved host session as pending. See the [agent walkthrough](../docs/docs/agents.md) or [MCP alternative](../docs/docs/mcp.md).

## Structure and maintenance

Each folder follows the [Agent Skills specification](https://agentskills.io/specification): descriptive YAML metadata and a focused `SKILL.md`, with task-specific `references/`, executable `scripts/` or authoring `templates/` only when useful. Entrypoints explain when to load each guide; read only the relevant material. Preserve complete folders so installed copies can use their bundled resources.

The [example brain](../examples/brain/README.md#review-a-decision) demonstrates decision review, retained evidence, consolidation and selected sharing with fictional data. The [workflow contribution checks](../CONTRIBUTING.md#test-outcomes-and-failures) cover disposable save/search/read/validate/evaluate runs; helper tests use synthetic inputs and verify failure behavior. Passing local checks does not prove host discovery, live source access or factual truth.

For development of the framework itself, use the repository-local [bf-contribute](../.agents/skills/bf-contribute/SKILL.md).
