# Getting started

By the end of this walkthrough, you will have a saved decision, a search that finds its reason and three checks that keep it retrievable. No sensor, account credentials or model is needed.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then Brain Framework. uv supplies Python 3.14 if needed. If `bf` is not on PATH, run `uv tool update-shell` and open a new shell.

```bash
uv tool install --python 3.14 'brain-framework==9.0.0'
bf --version
```

Create a brain. `init` also registers it in `~/.config/bf/config.yaml`, so `bf search` finds it from any directory and `bf update` may run its sensors on this machine.

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

## Join a team brain

Clone the team's private repository and register it. Run the search outside either brain to cover both, with each result labelled by its brain.

```bash
git clone git@github.com:team/knowledge.git ~/team-knowledge
bf register ~/team-knowledge
cd ~
bf search "release process"
```

Registration without `--collect` never runs the team's sensors on your laptop. Team records are usually collected by CI; see [personal and team brains](brain.md#personal-and-team-brains).

## Check the answers your team needs

Before adding integrations, try a small pilot: one project, one owner who keeps its note current, and three questions a teammate needs answered. Return to `~/knowledge`, where you saved the archive decision, and create `queries.yaml` there:

```yaml
# https://fmind.github.io/brain-framework/docs/search/
version: 3
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

Commit reviewed notes and retrieval cases to your private team repository through your normal review process. Keep personal records in a separate brain, and pass `--brain NAME` when selecting context for work. Start collecting only when a recurring question needs evidence the notes do not contain.

## Give agents access

Follow the [skill installation guide](https://github.com/fmind/brain-framework/blob/main/skills/README.md) to install `bf-use` in a directory your host discovers. Add `bf-learn` when you want the agent to maintain notes after work. Copy the whole skill folder, including any templates; installing the Python package does not install skills.

From a new agent session, ask: "Use the brain brain to find why we keep original evidence. Read the source and cite its ref." The agent should search that brain and read `projects/archive.md#decision` before answering. This checks that the host actually reaches your knowledge. Hosts that prefer tools can register `bf mcp --brain brain` instead; see [MCP](mcp.md).

For a work host, choose the team brain explicitly. An agent's model provider may receive retrieved text even though Brain Framework itself searches offline; see [separating audiences](privacy.md#separating-audiences).

## Try the example

The [runnable example](https://github.com/fmind/brain-framework/tree/main/examples/brain) contains a fictional project, OKF concept, resumable action, a credential-free sensor and retrieval cases. Follow its README in a disposable copy.

## Update Brain Framework

Read the [release notes](https://github.com/fmind/brain-framework/releases), then update the tool and check your brain:

```bash
uv tool install --upgrade --python 3.14 'brain-framework==9.0.0'
bf --version
bf validate --brain brain
bf eval --brain brain
```

Run `eval` once your brain has `queries.yaml`. Review and update separately installed skills, and restart an MCP host that still runs the old process. Brain Framework supports the current brain format only; historical breaking changes are recorded in the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md).

## Transition from FKF

Brain Framework 9 uses one current format (version 3). FKF 8 brains need a one-time transition before this package can read them: preserve the original files and machine state, pause collection, rename the brain layout and configuration, update authored links and integrations, carry over collection trust and successful-run windows, then rebuild and validate. Record payloads, IDs and provenance stay unchanged; pending transaction originals remain with `memories/`. There is no compatibility command or automatic migration.

The [v9 release notes](https://github.com/fmind/brain-framework/releases/tag/v9.0.0) list the interface changes. Earlier release notes retain the names and formats used at the time.

## When something is wrong

| Symptom                                          | Next step                                                                                                       |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| No brain selected                                | Run inside a brain, pass `--brain NAME`, or register one with `bf register PATH`.                               |
| A note or record is missing                      | `bf status` lists files the cache skipped and why; `bf validate` checks the whole brain.                        |
| A sensor reports duplicate IDs in stored records | Run `bf validate --brain NAME`, preserve both revisions and reconcile the named source before collecting again. |
| A source fails in `update`                       | `bf status` shows its last error and the path of its private stderr log.                                        |
| Collection refused                               | Review `sensors/` and `bf.yaml`, then grant trust on this machine with `bf register PATH --collect`.            |
| Search results look outdated                     | Another writer held the brain; results say `stale`. Retry, or run `bf build` to start over.                     |

If an operation reports a pending record transaction, preserve `memories/.pending/`, inspect the brain and run `bf build` to recover durable originals and rebuild. Ordinary search/read never apply a journal from an untrusted brain.
