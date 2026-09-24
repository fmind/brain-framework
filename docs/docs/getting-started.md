# Getting started

By the end of this walkthrough, you will have a saved decision, a search that finds its reason and three checks that keep it retrievable. No collector, account credentials or model is needed.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then FKF. uv supplies Python 3.14 if needed. If `fkf` is not on PATH, run `uv tool update-shell` and open a new shell.

```bash
uv tool install --python 3.14 'fkf==8.2.2'
fkf --version
```

Create a base. `init` also registers it in `~/.config/fkf/config.yaml`, so `fkf search` finds it from any directory and `fkf update` may run its collectors on this machine.

```bash
fkf init ~/knowledge --name brain
cd ~/knowledge && git init
fkf search welcome
fkf read wiki/welcome.md
```

Write one note per project in `projects/` and reusable knowledge in `wiki/`. Search notices edits by itself. Add a collector only when it answers a question you ask repeatedly; see [collectors](sources.md), then schedule `fkf update`.

## Save a decision

Create `projects/archive.md` in the base:

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
fkf search "providers delete old content"
fkf read projects/archive.md#decision
fkf validate
```

The result names the file and section; `read` returns that section directly. No indexing command is needed. As the project changes, update the note in place and let Git keep its history. Add dates, evidence links and retrieval cases as the note grows; see [notes](base.md#notes) and [retrieval cases](search.md#retrieval-cases).

## Join a team base

Clone the team's private repository and register it. Run the search outside either base to cover both, with each result labelled by its base.

```bash
git clone git@github.com:team/knowledge.git ~/team-knowledge
fkf register ~/team-knowledge
cd ~
fkf search "release process"
```

Registration without `--collect` never runs the team's collectors on your laptop. Team records are usually collected by CI; see [personal and team bases](base.md#personal-and-team-bases).

## Check the answers your team needs

Before adding integrations, try a small pilot: one project, one owner who keeps its note current, and three questions a teammate needs answered. Return to `~/knowledge`, where you saved the archive decision, and create `queries.yaml` there:

```yaml
# https://fmind.github.io/fkf/docs/search/
version: 2
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
fkf eval
fkf validate
```

The expected result is `"score":"3/3"` with `"passed":true`, and `"valid":true`. Replace these cases with your team's actual questions. Ask a teammate to repeat the walkthrough from a fresh clone and read the returned refs: retrieval checks establish that evidence is reachable; people still verify that it answers the question.

Commit reviewed notes and retrieval cases to your private team repository through your normal review process. Keep personal records in a separate base, and pass `--base NAME` when selecting context for work. Start collecting only when a recurring question needs evidence the notes do not contain.

## Give agents access

Follow the [skill installation guide](https://github.com/fmind/fkf/blob/main/skills/README.md) to install `fkf-use` in a directory your host discovers. Add `fkf-learn` when you want the agent to maintain notes after work. Copy the whole skill folder, including any templates; installing the Python package does not install skills.

From a new agent session, ask: "Use the brain base to find why we keep original evidence. Read the source and cite its ref." The agent should search that base and read `projects/archive.md#decision` before answering. This checks that the host actually reaches your knowledge. Hosts that prefer tools can register `fkf mcp --base brain` instead; see [MCP](mcp.md).

For a work host, choose the team base explicitly. An agent's model provider may receive retrieved text even though FKF itself searches offline; see [separating audiences](privacy.md#separating-audiences).

## Try the example

The [runnable example](https://github.com/fmind/fkf/tree/main/examples/base) contains a fictional project, OKF concept, resumable task, a credential-free collector and retrieval cases. Follow its README in a disposable copy.

## Update FKF

Read the [release notes](https://github.com/fmind/fkf/releases), then update the tool and check your base:

```bash
uv tool install --upgrade --python 3.14 'fkf==8.2.2'
fkf --version
fkf validate --base brain
fkf eval --base brain
```

Run `eval` once your base has `queries.yaml`. Review and update separately installed skills, and restart an MCP host that still runs the old process. FKF supports the current base format only; historical breaking changes are recorded in the [changelog](https://github.com/fmind/fkf/blob/main/CHANGELOG.md).

## Upgrading from v7

For the historical v7-to-v8 format change, see the [versioned upgrade notes](https://github.com/fmind/fkf/blob/v8.2.1/docs/docs/getting-started.md#upgrading-from-v7). Preserve the original base before any one-off conversion; the current package has no migration or compatibility command.

## When something is wrong

| Symptom                                             | Next step                                                                                                       |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| No base selected                                    | Run inside a base, pass `--base NAME`, or register one with `fkf register PATH`.                                |
| A note or record is missing                         | `fkf status` lists files the cache skipped and why; `fkf validate` checks the whole base.                       |
| A collector reports duplicate IDs in stored records | Run `fkf validate --base NAME`, preserve both revisions and reconcile the named source before collecting again. |
| A source fails in `update`                          | `fkf status` shows its last error and the path of its private stderr log.                                       |
| Collection refused                                  | Review `sources/` and `fkf.yaml`, then grant trust on this machine with `fkf register PATH --collect`.          |
| Search results look outdated                        | Another writer held the base; results say `stale`. Retry, or run `fkf build` to start over.                     |

If an operation reports a pending record transaction, preserve `records/.pending/`, inspect the base and run `fkf build` to recover durable originals and rebuild. Ordinary search/read never apply a journal from an untrusted base.
