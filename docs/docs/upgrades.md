# Versions and upgrades

## Install Brain Framework

Install the latest release with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install brain-framework
bf --version
```

Continue with [Getting started](getting-started.md) to create `~/brain` or another directory you choose and save your first decision.

## Update Brain Framework

Read the [release notes](https://github.com/fmind/brain-framework/releases) first. Within a major version, the brain format (`bf.yaml`, retrieval suites and folder layout) stays compatible. Major releases have manual upgrade steps in the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md); back up the brain and try those steps on a copy before adopting one.

After checking the target release and preparing any required format changes:

```bash
uv tool upgrade brain-framework
bf --version
bf validate --brain ~/brain
bf eval --brain ~/brain
```

`uv tool upgrade` installs the latest release, including a new major when one exists; check the release notes before running it. Validation checks files and links; evaluation checks your saved retrieval cases. Older brains without cases can add a [suite](checks.md) before running `bf eval`.

Review separately installed skills for updates, then restart MCP hosts so they load the updated package. Keep a team and its collection job on the same release.

## Upgrade an older brain

Work on a copy with schedules paused. Apply the manual steps for each intervening major release; there is no automatic migration. The checklist below covers the main areas to review, while the changelog owns the version-specific instructions.

### Review configuration and collection

Remove obsolete sensor `trust`, registry `collect` and `--collect`/`--no-collect` options. Registration now stores names and paths only.

Give every scheduled command an explicit root:

```bash
bf update --brain ~/brain
```

An unqualified `bf update` outside a brain can run due programs in every registered root. Review sensor and routine scripts before resuming those jobs.

### Keep metadata on every action

Projects, concepts and `actions/YYYY-MM-DD_slug/ACTION.md` need valid [OKF metadata](brain.md#notes), including a nonempty `type`. An action entry can start like this:

```markdown
---
type: action
title: Website review
status: draft
updated: 2026-09-27
---

# Website review

Work is blocked until the product-page draft is available.

- [ ] Review the page against the website decision.
```

Use `draft`, `stable` or `deprecated` for the note's status. Keep work progress such as active, blocked or done in its body or task list. Marking a reviewed note `stable` does not complete its open tasks. Update routine scripts and action templates to emit the metadata before resuming updates.

### Check retrieval consumers

Update CLI/MCP consumers for removed fields and [pagination and large reads](retrieval.md#continuations). Replace entities or aliases under `bf://NAME/tags/` with ordinary links, and review [tag labels](schema.md#tag-rules).

### Verify before resuming schedules

Validate and evaluate the copy, then check health on the collecting machine:

```bash
bf validate --brain ~/brain
bf eval --brain ~/brain
bf status --check --brain ~/brain
```

Substitute the copy's path while testing it. A shared clone reports scheduled sources as `never` until it collects them locally; passing validation does not establish local collection health.
