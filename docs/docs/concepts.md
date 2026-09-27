# Core concepts

A brain keeps the context needed to continue work: what you decided, why, what supports it and what to do next. It is an ordinary directory you and your agents can read across sessions. Start with a note; add collection when a recurring question needs evidence from another tool.

## Four places to keep knowledge

| Kind        | Question it answers                | Example in a website project                                              |
| ----------- | ---------------------------------- | ------------------------------------------------------------------------- |
| **Project** | What are we trying to do, and why? | “Build one product page. Next: draft it.”                                 |
| **Memory**  | What did the source say?           | The collected product brief, with its source URL.                         |
| **Concept** | What can we reuse elsewhere?       | “Explain the product before asking for signup,” with evidence and limits. |
| **Action**  | Where did this session get to?     | A page review's objective, findings and next step.                        |

Projects, concepts and action entry notes use [Open Knowledge Format (OKF)](brain.md#notes): Markdown with YAML metadata. Memories are records stored as JSON Lines: one JSON object per line. A project can span many actions; a concept can be useful to many projects. A memory preserves evidence but does not make its contents true.

## Follow one decision

Suppose a team asks, “Why did we choose a single product page?” The [New website walkthrough](getting-started.md) saves this answer in `projects/new-website.md`:

```markdown
## Decision

Start with a single product page because visitors need a clear explanation before signing up.

## Next actions

- [ ] Draft the product page.
```

```bash
bf search "visitors clear explanation"
bf read projects/new-website.md#decision
```

Search returns the Decision section's address; read returns the original words. As the work grows, add only the files you need:

| Save…                          | In…                                                | So that…                                                  |
| ------------------------------ | -------------------------------------------------- | --------------------------------------------------------- |
| The product brief              | A [collected record](sensors.md#your-first-sensor) | The decision can link to the source it relies on.         |
| The page review                | `actions/2026-09-27_website-review/ACTION.md`      | Another session can read its Resume section and continue. |
| Advice supported by the review | `concepts/explain-before-signup.md`                | Another project can reuse it and inspect its limits.      |

The [file examples](brain.md) show complete notes. A reviewed action should update the project's current state and next task; BF does not do that automatically. People and agents judge the evidence, perform authorized work and record what they learned.

## Sensors and routines

A **sensor** gathers selected evidence and prints JSON records. For example, the [local-document sensor](sensors.md#your-first-sensor) turns a product brief into a searchable record; BF validates and saves it.

A **routine** is a deterministic maintenance script. A [configured weekly review](routines.md) lists projects needing attention in a new OKF action. Configured routines run through `bf update`; general upkeep scripts, such as backups, run directly or through your scheduler.

Sensors and configured review routines are optional programs. Collection happens when you run `bf collect` or `bf update`, or when your own timer runs an update. Search and read never start them.

## Find evidence and follow connections

`bf search` finds words or an explicitly declared identity. Its **ref** is an address such as `projects/new-website.md#decision`, which `bf read` opens exactly.

When a concept links to a project's decision, reading the project shows a **backlink** to the concept. A **relationship** adds a declared meaning, such as “depends on.” A **claim** retains that relationship and the section or record that asserted it. These explicit connections form the knowledge graph; similar names alone never create links. Try [connecting two notes](links.md).

The files remain authoritative. The local SQLite database in `.bf/` is a disposable search cache that rebuilds from them.

Next: [write your first decision](getting-started.md) or learn [where each file belongs](brain.md). Use the [glossary](glossary.md) to look up a term later.
