# Evidence, revisions and bounded impact

Use when a consequential decision must stay explainable after its sources change, or when reviewing an explicit dependency. Capture only the selected evidence that decision needs, never the whole brain.

## Triage a selected passage

Read the exact highlight record, its source URL and page or section before promoting it into a project or concept. In the highlights sensor, `text` is the selected passage, `attributes.annotation` is a separate interpretation, `time` is the supplied capture time and `attributes.source_date` is the source's stated date. Neither collection nor an annotation establishes truth or authority. Check enough surrounding context to support the intended claim; if the original is unavailable, keep that limitation.

Choose one outcome: link the passage from an existing decision or lesson; keep it as unreviewed evidence with its open question; or exclude it from the selected export during an authorized cleanup. Keep the conclusion in the owning note, cite the highlight ref and separate quotation from interpretation. Before a replacement or cleanup removes a revision supporting a decision, retain it as below. Do not copy a whole document to triage one passage.

## Retain a revision

Read the exact note section or record with `bf read`. Record its original ref, when it became known, when the claim applies (if stated) and any dispute in a dated decision note. Use an existing action's `outputs/` and `inputs/` when that action owns the work; otherwise keep the dated note under `projects/` and the capture under `assets/`, linked from the owning project. Never create an action only to retain evidence. The capture time is when this local copy was made, not necessarily when the underlying event happened.

The standard-library [evidence helper](../scripts/evidence.py) never contacts a network. `read REF --brain PATH` runs the offline `bf read` and prints the whole reply: above 32 KiB it follows each `next_offset`, checks that every page names the same file `sha256` (and, for a whole note, that the joined text has it) and joins the pages, up to 4 MiB. `capture` and `compare` read only stdin. `capture` keeps one exact read's text or record, its source metadata, a local capture time and a content digest; it drops backlinks and other context and refuses listing pages, lone text pages and incomplete or `stale` reads. Partial or not-fresh records keep explicit limitations. A capture is a local observation, not proof of authorship, truth or live provider state.

In the commands below, `$evidence_helper` is the path of `scripts/evidence.py` in this skill's folder, `$brain_path` the brain directory and `$evidence_ref` the exact ref; with several selected brains, use the returned `uri` (`bf://NAME/...`), since a plain ref present in two brains fails. This subshell creates an owner-only capture at an unused path, never replaces an earlier one and removes only a capture it failed to write:

```bash
(
  set -o pipefail -o noclobber
  umask 077
  exec 3> "$new_capture" || exit 1
  python3 "$evidence_helper" read "$evidence_ref" --brain "$brain_path" |
    python3 "$evidence_helper" capture >&3 || { rm -f -- "$new_capture"; exit 1; }
)
```

Captures may contain private or external text: give them the original's audience and backup protection. Action inputs and assets are not ignored by Git by default; inspect the brain's ignore rules before committing. Capture JSON is not indexed, but anyone with file access can read it; use an approved private location when a shared brain's audience is too broad. Cite the original ref and the relative capture file from the decision note when both belong in that brain. A later record update or deletion cannot reconstruct a revision nobody captured.

To compare, supply the saved capture followed by a new exact read:

```bash
(
  set -o pipefail
  {
    cat "$saved_capture"
    python3 "$evidence_helper" read "$evidence_ref" --brain "$brain_path"
  } | python3 "$evidence_helper" compare
)
```

Only the compact comparison reaches the model. `state` is `changed`, `unchanged` or `unknown`; `content_changed` compares stored content even when freshness is unknown. A new `observed` time alone is not a change; every other record field is. A partial baseline, a partial record, an incomplete read or a source that is not both `active` and `fresh` (including `overdue`, `never`, `manual`, disabled and historical sources) makes the state `unknown`. A missing read fails; it never means unchanged. For a record, `unchanged` means no newer local revision: a window sensor re-reads only recent windows, so an older item can change upstream unseen, while a snapshot sensor re-reads its whole catalog each run. The digest detects accidental alteration of the capture; it is not a signature. The helper caps stdin at 9 MiB and prints fixed errors without private excerpts; rerun a read that changed while it was assembled.

## Revise a belief

Keep the current conclusion in the owning project or concept. When the old claim matters, keep its decision note and capture, then write a successor decision note linking what it supersedes and why. A decision note under `outputs/` is ordinary Markdown: its `type`, `status` and `updated` apply, and its typed links use the file as their subject. Use `status: deprecated` for an authored note intentionally retired, and say "disputed" in the text when evidence conflicts: disputed is not a status. Distinguish disagreement from supersession and a fact from a proposal; more recent evidence is not automatically more authoritative.

Declare only the relations you use, such as `supersedes`, under `fields:` in `bf.yaml` (see [links](links.md)). Then write `[evidence](bf://NAME/concepts/policy.md?rel=depends-on#retention)` and `[previous decision](bf://NAME/actions/YYYY-MM-DD_topic/outputs/decision.md?rel=supersedes)`. Generic links establish neither dependency nor supersession. State when a claim holds as prose unless the brain declares fields for it. BF resolves these links; it never infers temporal truth or chooses the winning claim.

## Review impact lightly

When selected evidence changes, list its dependents with `bf read 'REF' --rel depends-on` on the whole note, record or identity (a section has no relation page), following `next_offset`: a whole read's backlink group previews only 5. For each dependent, read it and check its `depends-on` `claims`: `target` tells a dependency on the changed section from one on another section, and `origin` names the section making the claim. `bf search 'IDENTITY'` gives the same `relations` for every linking item at once. Review direct dependents first, then at most one more layer: **two hops, ten distinct dependents, one visit per qualified ref**, which also stops cycles. Equal relation names in different brains need their declared meanings checked before you follow them.

For each affected conclusion, report the dependency path and whether it needs review, remains justified after inspection or lacks evidence. Change alone never proves falsity, and generic or `supersedes` links do not propagate impact. An unfollowed `next_offset`, truncated claims, a missing brain or an exhausted limit makes the review explicitly incomplete; never silently clear the remaining dependents. Reuse a previous review when both the evidence digest and the conclusion are unchanged. This is an agent review over graph reads, not an automatic invalidation engine.
