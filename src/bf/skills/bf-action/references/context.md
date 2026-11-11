# Working context

Keep `## Context {#context}` within **300 words, six evidence refs and 4 KiB of UTF-8**, and `## Resume {#resume}` within **100 words** (last verified state, blocker and exact next step).

Context holds only the requested outcome, constraints, active project decision, material unknowns and refs needed next; put artifacts in `inputs/` and `outputs/`, never transcripts:

```markdown
## Context {#context}

Draft a product page explaining the service before signup, following [project decision](../../projects/new-website.md#decision). Include audience, benefit and example; pricing remains open. Save draft to outputs/product-page.md.

## Resume {#resume}

Objective and decision recorded; no draft yet. Next: read the decision and outline the page, keeping pricing as an open question.
```

Use brain-qualified `uri` refs when multiple brains are selected, and refresh both sections in place when state changes ([handoff](handoff.md)).

## Read on resume {#read-on-resume}

After `#context`, `#resume` and the owning project's `#decision`, read only the refs the next step depends on (one link hop, at most six supporting reads) and flag any remaining gap before widening scope.
