# Evidence, revisions and bounded impact

Use when a consequential decision must remain explainable after its sources change, or when reviewing an explicit dependency. Capture only the selected evidence needed for that decision, not the whole brain.

## Retain a revision

Read the exact note section or record through `bf read`. Record its original ref, when it became known, when the claim applies (if stated), and any dispute in a dated decision note in an existing action's `outputs/`. Keep these dates distinct; the capture time is when this local copy was made, not necessarily when the underlying event happened.

The standard-library [evidence helper](../scripts/evidence.py) runs with Python 3.11 or later, consumes JSON on stdin and never opens a brain, invokes a command, or contacts a network. `capture` accepts one exact read and retains its text or record, source metadata, timestamp and content digest. It excludes backlinks and other context. It refuses pages and incomplete reads. Partial or non-fresh records retain explicit limitations. This is a local observation, not proof of authorship, truth or live provider state.

From Bash, with the brain, exact ref, helper path and an unused destination under the authorized action's `inputs/`. The subshell keeps the options local, creates the capture owner-only, never replaces an earlier capture and removes only a capture it failed to write:

```bash
(
  set -o pipefail -o noclobber
  umask 077
  test ! -e "$new_capture" || { echo "keep the existing capture" >&2; exit 1; }
  bf read "$evidence_ref" --brain "$brain_path" |
    python3 "$evidence_helper" capture > "$new_capture" || { rm -f -- "$new_capture"; exit 1; }
)
```

Captures may contain private or external text: keep the same audience and backup protection as the original. Action inputs are not universally Git-ignored; inspect the brain's ignore rules before committing. Their JSON is not indexed, but anyone with filesystem access can still read it. Cite the original ref and the relative capture file from the decision note. A record upsert or deletion cannot reconstruct an uncaptured past revision.

To compare, supply the saved capture followed by a new exact read as two JSON documents:

```bash
(
  set -o pipefail
  {
    cat "$saved_capture"
    bf read "$evidence_ref" --brain "$brain_path"
  } | python3 "$evidence_helper" compare
)
```

Only the compact comparison reaches the model. `state` is `changed`, `unchanged` or `unknown`; `content_changed` compares stored content even when freshness is unknown. A new `observed` timestamp alone does not count as changed evidence; all other record fields do. Partial baselines, partial records, incomplete reads and any source that is not both `active` and `fresh` (including `manual`, disabled and historical sources) prevent a definitive state. Missing reads fail, never mean unchanged. For a record, `unchanged` means no newer local revision: a window sensor re-reads only its recent windows, so an older item can change upstream without a new local revision, while a snapshot sensor re-reads its whole catalog on each run. A digest detects accidental alteration of the capture; it is not a signature. The helper caps input at 9 MiB and emits generic errors without private excerpts.

## Revise a belief

Keep the current conclusion in the owning project or concept. If the old claim matters, preserve its decision note and capture, then create a successor decision note linking what it supersedes and why. Use `status: deprecated` for an authored note intentionally retired; use "disputed" in its text when evidence conflicts, since disputed is not a core status. Distinguish disagreement from supersession and a factual claim from a proposal. More recent evidence is not automatically more authoritative.

For machine-readable relationships, declare only the roles you use in `bf.yaml`; their meanings are local conventions. For example:

```yaml
# https://fmind.github.io/brain-framework/docs/schema/
schema:
  depends-on:
    description: This conclusion needs review when the target evidence changes.
    type: identity
    cardinality: many
    relation: true
  supersedes:
    description: This authored decision explicitly replaces the target decision.
    type: identity
    cardinality: many
    relation: true
```

Use `[evidence](bf://NAME/path.md?rel=depends-on#stable-section)` and `[previous decision](bf://NAME/actions/DATE_slug/outputs/decision.md?rel=supersedes)`. Generic links do not establish dependency or supersession. Store truth dates as explicit prose unless the brain has declared corresponding schema fields. The framework resolves these links; it does not infer temporal truth or choose the winning claim.

## Review impact lightly

When selected evidence changes, read its backlinks and inspect only the declared `depends-on` group. Section reads intentionally omit backlinks: read the parent note for this step, then check each claim's `target` to distinguish a dependency on the changed section from one on another section. Read each edge's `origin` and supporting evidence. Review direct dependents first, then at most one more dependency layer: **two hops, ten distinct dependents, one visit per qualified ref**. Stop cycles with that visited set. Equal role names in different brains need their schema meanings checked before following them.

For each affected conclusion, report the dependency path and whether it needs review, remains justified after inspection, or lacks enough evidence. Change alone never proves falsity. Generic links and `supersedes` links do not propagate impact. A truncated backlink list, missing brain or exhausted limit produces an explicit incomplete-review note; do not silently clear the remaining dependents. Reuse a previous review when both the evidence digest and conclusion are unchanged. This is an agent review over existing graph reads, not an automatic invalidation engine.
