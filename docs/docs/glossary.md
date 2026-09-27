# Glossary

Find a term here, then follow its guide for a worked example. Read [Core concepts](concepts.md) first if the vocabulary is new.

In the website example, the **project** keeps the decision, an **action** records a review session, and a **concept** preserves what you learned. A **sensor** can collect the brief as a **record**. Search returns a **ref** that you read to check the evidence.

| Term            | Meaning                                                                                                                       | Read more                                                                   |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Action          | One session of work recorded in an OKF `ACTION.md`, with inputs, outcome and next step.                                       | [Write and resume an action](brain.md#actions)                              |
| Alias           | Another identity explicitly declared for the same subject, such as its repository URL.                                        | [BF links](link-reference.md#bf-links)                                      |
| Backlink        | An incoming link: the website decision lists the review action that links to it.                                              | [Connect two notes](links.md#connect-two-notes)                             |
| Brain           | A directory of authored notes, collected evidence and configuration.                                                          | [Files and notes](brain.md)                                                 |
| Claim           | A relationship plus its source: for example, a commit credits Alice as `author`. The source is evidence, not proof.           | [Relationship links](link-reference.md#relationship-links)                  |
| Concept         | Reusable knowledge with evidence and limits, such as why to explain a product before asking for signup.                       | [Write a concept](brain.md#concepts)                                        |
| Coverage        | The collection windows retained in this machine's run history; not a guarantee that the provider returned every item.         | [Backfills and coverage](sensors.md#backfills-and-coverage)                 |
| Entity          | The subject a note explicitly represents, with its own stable identity.                                                       | [Give a subject an identity](links.md#give-a-subject-a-stable-identity)     |
| Frontmatter     | YAML metadata between `---` lines at the start of a Markdown note, such as its `type`, `status` and `sources`.                | [Notes](brain.md#notes)                                                     |
| Freshness       | How recent local collection success is relative to a source's schedule. It does not establish that the provider is unchanged. | [Completeness and freshness](retrieval.md#incomplete-answers-and-freshness) |
| Identity        | An exact identifier such as a BF address or declared repository alias. Similar names do not establish equivalence.            | [Notes, records and identities](retrieval.md#notes-records-and-identities)  |
| Memory / record | One collected source item, saved as a JSON file with a stable id.                                                             | [Records](brain.md#records)                                                 |
| OKF             | Open Knowledge Format v0.2: Markdown and metadata conventions for projects, concepts and `ACTION.md` notes.                   | [Notes](brain.md#notes)                                                     |
| Page            | A computed view returned by `bf read`, such as projects, recent activity or source coverage.                                  | [Browse pages](search.md#pages)                                             |
| Project         | A maintained note describing a goal, current state, decisions and next tasks.                                                 | [Notes](brain.md#notes)                                                     |
| Ref             | An address for `bf read`, such as `projects/new-website.md#decision`. A `uri` also names its brain.                           | [Search and read](search.md)                                                |
| Relationship    | The declared meaning of a link, such as `depends-on` or `author`.                                                             | [Name a relationship](links.md#name-a-relationship)                         |
| Retrieval case  | A saved question and the evidence that search or read must return.                                                            | [Check your brain](checks.md#retrieval-cases)                               |
| Routine         | A deterministic maintenance script; routines configured in `bf.yaml` produce review actions.                                  | [Routines](routines.md)                                                     |
| Schema          | The declared vocabulary and validation rules for shared record fields and relationship roles.                                 | [Schema mappings](schema.md#shared-fields-and-sensor-mappings)              |
| Scope           | Where a search looks: for example, `--scope projects` or `--scope 7d`.                                                        | [Search reference](retrieval.md#search)                                     |
| Sensor          | A program that gathers selected evidence and prints JSON records for collection.                                              | [Your first sensor](sensors.md#your-first-sensor)                           |
| Stale cache     | A search cache that could not refresh because a writer held its lock. Separate from source freshness.                         | [Incomplete answers](search.md#incomplete-answers-and-freshness)            |
| Tag             | An explicit topic label grouping authored notes across folders; not a dependency or identity alias.                           | [Tags](brain.md#tags)                                                       |
