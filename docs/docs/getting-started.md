# Getting started

By the end of this walkthrough, you will have a saved decision, a search that finds its reason and three checks that keep it retrievable. No sensor, account credentials or model is needed.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then Brain Framework. uv supplies Python 3.14 if needed. If `bf` is not on PATH, run `uv tool update-shell` and open a new shell.

```bash
uv tool install --python 3.14 'brain-framework==10.0.0'
bf --version
```

Create a brain. No global configuration is required: work inside its directory or pass `--brain PATH`. Collection trust is opt-in with `bf register PATH --collect`.

```bash
bf init ~/knowledge --name brain
cd ~/knowledge && git init
bf search welcome
bf read concepts/welcome.md
```

Write one note per project in `projects/` and reusable knowledge in `concepts/`. Search notices edits by itself. Add a sensor only when it answers a question you ask repeatedly; see [sensors](sensors.md), then schedule `bf update`.

## Save a decision

Create `projects/archive.md` in the brain:

```markdown
---
type: project
status: active
summary: Keep original evidence so decisions remain explainable.
---

# Archive

## Decision

Keep original evidence because providers may delete old content.

## Next actions

- [ ] Document the retention policy.
```

```bash
bf search "providers delete old content"
bf read projects/archive.md#decision
bf validate
```

The result names the file and section; `read` returns that section directly. No indexing command is needed. As the project changes, update the note in place and let Git keep its history. Add dates, evidence links and retrieval cases as the note grows; see [notes](brain.md#notes) and [retrieval cases](search.md#retrieval-cases).

## Connect your knowledge

New brains declare `author`, `owner`, `depends-on` and `related-to` relationships in `bf.yaml`, and their generated `AGENTS.md` teaches agents to use them. Keep that brain's `name` stable across machines; it is the authority of portable BF addresses.

In the archive note above, add `entity: bf://brain/projects/archive` to its frontmatter. Add this link under its Decision section:

```markdown
[Welcome guide](bf://brain/concepts/welcome.md?rel=related-to)
```

```bash
bf search --subject bf://brain/projects/archive --relation related-to
bf search --target bf://brain/concepts/welcome.md
bf read 'bf://brain/projects/archive.md#decision'
bf validate
```

The results identify the containing decision section as the relationship's origin and evidence. Use `## Decision {#decision}` if that anchor must survive later wording changes. For people, use a logical entity identity such as `bf://brain/people/marc` on a note in an existing authored folder; no `people/` directory is needed. Add only reviewed aliases and relationships. See the [full link contract](schema.md#bf-links).

## Join a team brain

Clone the team's private repository and search it directly. To include it in personal searches, add `brains: {team-knowledge: {path: ../team-knowledge}}` to your personal brain's `bf.yaml` when the two directories are siblings.

```bash
git clone git@github.com:team/knowledge.git ~/team-knowledge
cd ~/team-knowledge
bf search "release process"
```

Reading a clone or declaring a reference never runs the team's sensors on your laptop. Team records are usually collected by CI; see [team brains](team.md) to create one or resolve a name already taken on your machine.

## Check the answers your team needs

Before adding integrations, try a small pilot: one project, one owner who keeps its note current, and three questions a teammate needs answered. Return to `~/knowledge`, where you saved the archive decision, and create an `evals/` directory and `evals/retrieval.yaml` there:

```yaml
# https://fmind.github.io/brain-framework/docs/search/
version: 4
cases:
  - name: find-the-reason
    query: providers delete old content
    expect: [projects/archive.md#decision]
    text: [providers may delete old content]
  - name: find-the-next-action
    query: retention policy
    expect: [projects/archive.md#next-actions]
    text: [Document the retention policy]
  - name: avoid-an-unrelated-answer
    query: nonexistent-pilot-topic
    empty: true
```

```bash
cd ~/knowledge
bf eval
bf validate
```

The expected result is `"score":"3/3"` with `"passed":true`, and `"valid":true`. Replace these cases with your team's actual questions. Ask a teammate to repeat the walkthrough from a fresh clone and read the returned refs: retrieval checks establish that evidence is reachable; people still verify that it answers the question.

Commit reviewed notes and retrieval cases to your private team repository through your normal review process; create that brain with a team-specific name as described in [team brains](team.md#create-it). Keep personal records in a separate brain, and pass `--brain NAME` when selecting context for work. Start collecting only when a recurring question needs evidence the notes do not contain.

## Give agents access

Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md) to install `bf-use` in a directory your host discovers. Add `bf-learn` when you want the agent to maintain notes after work. Copy the whole skill folder, including any templates; installing the Python package does not install skills.

From a new agent session, ask: "Search my brain for why we keep original evidence. Read the source and cite its ref." The agent should search that brain and read `projects/archive.md#decision` before answering. This checks that the host actually reaches your knowledge. Hosts that prefer tools can register `bf mcp --brain ~/brain` instead; see [MCP](mcp.md).

For a work host, choose the team brain explicitly. An agent's model provider may receive retrieved text even though Brain Framework itself searches offline; see [separating audiences](privacy.md#separating-audiences).

## Try the example

The [runnable example](https://github.com/fmind/brain-framework/tree/main/examples/brain) contains a fictional project, OKF concept, resumable action, a credential-free sensor and retrieval cases. Follow its README in a disposable copy.

## Update Brain Framework

Read the [release notes](https://github.com/fmind/brain-framework/releases), then update the tool and check your brain:

```bash
uv tool install --upgrade --python 3.14 'brain-framework==10.0.0'
bf --version
bf validate --brain brain
bf eval --brain brain
```

Run `eval` once your brain has `evals/retrieval.yaml`. Review and update separately installed skills, and restart an MCP host that still runs the old process. Within a major version, the brain format (`bf.yaml`, `evals/retrieval.yaml` and the folder layout) stays compatible. A new major version supports only its current format and documents manual upgrade steps; historical breaking changes are recorded in the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md).

## Transition from FKF

Brain Framework 10 uses version 4 of `bf.yaml` and retrieval suites under `evals/`. Before upgrading from 9, preserve the original files and runtime, pause collection, update every reader and writer together, change configuration and suite versions to 4, and move `queries.yaml` to `evals/retrieval.yaml`. Add the shared schema and explicit sensor mappings; existing records without `fields` remain readable but have no typed graph evidence. Rebuild, validate and evaluate before resuming collection. Follow the complete manual steps in the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md). There is no compatibility command or automatic migration.
