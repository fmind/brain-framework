# Working context

Keep `## Context {#context}` in `ACTION.md` within **300 words, six evidence refs and 4 KiB of UTF-8**, and `## Resume {#resume}` within 100 words: the last verified state, the blocker and the next step. These are writing budgets, not targets.

Context holds only the requested outcome, binding constraints, the current decision, material unknowns and the refs the next step needs. Put long evidence and artifacts in `inputs/` and `outputs/`; never paste transcripts or the project's history. For example:

```markdown
## Context {#context}

Draft a product page that explains the service before asking visitors to sign up. Follow the [project decision](../../projects/new-website.md#decision). The draft needs a clear audience, benefit and example; pricing is still undecided. Save the draft in outputs/product-page.md for review.

## Resume {#resume}

The objective and project decision are recorded. No draft exists yet. Next: read the decision and outline the page; leave pricing as an explicit open question.
```

With several brains, keep refs brain-qualified (`uri`). Name external evidence by ref and say why it matters instead of copying it; incomplete or stale evidence belongs in the unknowns. Refresh both sections in place only when the outcome, constraints, evidence or next step change. The [handoff checker](handoff.md) measures them; count the refs and judge readiness yourself.

## Read on resume

After Context, Resume and the owning project's current decision, open only the evidence the next step depends on, following at most one layer of links. Stop after six supporting reads, and name a remaining consequential gap before widening the scope.
