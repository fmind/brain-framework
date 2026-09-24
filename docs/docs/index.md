# FKF

**Keep the context. Continue the work.**

FKF gives you and your agents a shared memory in files you own. Find why a decision was made, resume a project after a break, or give a teammate the evidence they need to act. The same knowledge stays available when you change editors or agent hosts.

Markdown notes hold decisions and reusable knowledge; JSON Lines records hold what optional collectors gathered from selected sources. FKF searches both offline and returns readable refs to the original files. You or your agent interpret the evidence and update the notes after work.

Start with one decision worth remembering. Search it, read its source, and keep the note current as the project changes. Collectors are optional: a shared repository of useful notes is already a working knowledge base.

## Small parts, ordinary tools

Use your editor for notes, Git for review, provider CLIs for authentication and a native timer for collection. FKF contributes one command, a rebuildable SQLite cache and two read-only MCP tools. It requires no model, hosted database or background server. Search matches words, explicit identities and dates; it does not generate answers or automatically learn from conversations.

These docs describe FKF 8.2.2. Read the [changelog](https://github.com/fmind/fkf/blob/main/CHANGELOG.md) for release changes. Check `fkf --version` and follow the [installation guide](getting-started.md) to update an older copy.

## Start with the question you need to answer

| You want to…                               | Start here                                                                                                                             |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Remember why a decision was made           | [Write and retrieve your first decision](getting-started.md#save-a-decision).                                                          |
| Give a teammate enough context to continue | [Join a team base](getting-started.md#join-a-team-base) and [check its answers](getting-started.md#check-the-answers-your-team-needs). |
| Find recent work or an exact source        | [Search by words, identity or time](search.md).                                                                                        |
| Bring recurring evidence into the base     | [Add a scoped collector](sources.md).                                                                                                  |
| Let an agent use the same knowledge        | [Install a workflow skill](getting-started.md#give-agents-access) or connect the [MCP tools](mcp.md).                                  |

The [base layout](base.md), [command reference](commands.md), [configuration schema](schema.md) and [security model](privacy.md) cover the details when you need them.
