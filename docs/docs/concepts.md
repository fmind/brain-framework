# Core concepts

A **brain** is a folder of evidence and notes you own. People and agents use it to remember why decisions were made, find current work and carry useful context into the next session.

## Four kinds of knowledge

| Kind    | What belongs here                                                | Example                              | Location    |
| ------- | ---------------------------------------------------------------- | ------------------------------------ | ----------- |
| Project | Goals, current state, decisions and next tasks.                  | “Build one page; draft it next.”     | `projects/` |
| Memory  | What a selected source said, with its identity and provenance.   | A collected product brief.           | `memories/` |
| Concept | Knowledge reusable beyond one project, with evidence and limits. | “Explain before asking for signup.”  | `concepts/` |
| Action  | One work session's objective, results and next step.             | A website review to resume tomorrow. | `actions/`  |

Projects, concepts and action entry notes are Markdown with YAML metadata ([OKF](brain.md#notes)). Memories are JSON records. A memory is evidence of what a source said, not proof that it is true.

## From source to decision

**Gather → normalize → organize and connect → act → learn.**

| Step                 | What happens                                                            | Who does it                                    |
| -------------------- | ----------------------------------------------------------------------- | ---------------------------------------------- |
| Gather               | A sensor reads a selected brief and emits a record.                     | A small script you configure.                  |
| Normalize            | A schema mapping adds a shared field such as `kind: document`.          | BF during collection.                          |
| Organize and connect | A project links its decision to the brief.                              | You or your agent; BF indexes explicit links.  |
| Act                  | Review the page and record the result in an action.                     | You or your agent with authorized tools.       |
| Learn                | Update the project's conclusion; retain a reusable lesson as a concept. | You or your agent after reviewing the outcome. |

[Getting started](getting-started.md) runs the first three steps using one local file. BF supplies storage and retrieval; it does not run a model or make judgments.

## Programs and automation

| Concept          | Purpose                                                  | Example                                         |
| ---------------- | -------------------------------------------------------- | ----------------------------------------------- |
| Sensor           | Extract selected source evidence into records.           | Read a brief from a local text file.            |
| Schema           | Define common fields and explicit relationship meanings. | `kind` is a string; `author` is an identity.    |
| Mapping          | Connect sensor output to a schema field.                 | `/attributes/author_refs` → `author`.           |
| Routine          | Produce a review action through a deterministic program. | List projects needing attention.                |
| Watch / schedule | Check when configured programs are due.                  | `bf watch` runs while your terminal stays open. |

Search and read never start these programs. [Sensors](sensors.md), [schema](schema.md), [routines](routines.md) and [scheduling](schedule.md) explain their setup.

## Retrieval and connections

| Concept              | Plain meaning                                                   | Example                                             |
| -------------------- | --------------------------------------------------------------- | --------------------------------------------------- |
| Ref                  | An exact address to read.                                       | `projects/new-website.md#decision`                  |
| Page                 | A computed view of saved files.                                 | `bf read tasks` lists open checkboxes.              |
| Scope                | Where a search should look.                                     | `bf search "explanation" --scope projects`          |
| Tag                  | A topic label explicitly added to a note.                       | `tags: [website]`                                   |
| Entity / alias       | A stable subject identity and verified alternate identifiers.   | A project and its repository id.                    |
| Link / backlink      | An outgoing reference and the incoming view at its destination. | A concept cites a project decision.                 |
| Relationship / claim | A link's declared meaning, plus the source asserting it.        | An action `depends-on` a decision.                  |
| Knowledge graph      | The collection of these explicit connections.                   | Read a person to see records naming them as author. |

[Linking knowledge](links.md) shows how to add these connections. Similar names never establish identity, and BF does not infer relationships from prose.

## What stays authoritative

- **Files:** editable notes and records are the source of truth for the brain.
- **Cache:** `.bf/` is disposable SQLite data rebuilt from those files.
- **Source coverage:** tells you what was collected and whether local collection is fresh; a fresh cache can contain old evidence.
- **Retrieval cases:** saved questions and expected refs check that useful answers remain findable.
- **Skills:** procedures an agent follows; **MCP:** a way for its host to call BF's search and read tools.
- **Related brains:** directly declared local folders included in retrieval; they are not automatically collected or downloaded.

Next: [create a brain](getting-started.md), [connect an agent](agents.md), or [set up a team](team.md). Use the [glossary](glossary.md) to look up individual terms.
