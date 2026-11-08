# Evidence, revisions and bounded impact

Use when a consequential decision must stay explainable after its sources change, or when reviewing an explicit dependency. Capture only the selected evidence that decision needs, never the whole brain.

A search result or record read marked `"generated":true` is an assistant's model output: never cite it as the source of a fact; read and cite what it cites.

## Triage a selected passage

Read the exact highlight record and its source URL before promoting it: `text` is the selected passage, `attributes.annotation` a separate interpretation, `time` the capture time and `attributes.source_date` the source's stated date. Neither establishes truth: check enough surrounding context, or keep that limitation. Link the passage from an existing decision or lesson, keep it as unreviewed evidence with its open question, or exclude it during an authorized cleanup after retaining any revision a decision relies on, separating quotation from interpretation.

## Retain a revision

Read the exact note section or record with `bf read`. In a dated decision note, in the owning action's `outputs/` or otherwise under `projects/`, record its original ref, when it became known, when the claim applies and any dispute; never create an action only to retain evidence. Store the capture where the evidence's Git policy puts it:

- A record of a source Git ignores, as `bf init` ignores `memories/`, goes in the ignored `originals/` folder; cite its path in a code span, since a link fails `bf validate` in clones that lack the file.
- An authored note, or a record Git versions, goes in the owning action's `inputs/`, otherwise in `assets/`.

The [evidence helper](../scripts/evidence.py) never contacts a network. `read REF --brain PATH` prints the whole exact reply of the offline `bf read`. `capture` keeps one exact read's text or record, source metadata, local capture time and a content digest, and refuses listing pages, lone text pages and incomplete or `stale` reads: a local observation, not proof of truth or live provider state. A capture or saved reply may hold at most 19 MiB; beyond that, capture a note section or a smaller record.

Capture the exact ref (with several brains, its `uri`) into `NEW.json`, an unused path in an existing folder chosen as above:

```bash
python3 SKILL_DIR/scripts/evidence.py capture 'REF' --brain PATH --output NEW.json
```

It replies `{"captured":"NEW.json"}` and writes an owner-only file, never replaces an existing one and keeps nothing when the read fails or is incomplete. Captures may hold private or external text: give them the original's audience, and check the brain's ignore rules before committing action inputs or assets.

To compare, pass the saved capture (`SAVED.json`), then the same ref:

```bash
python3 SKILL_DIR/scripts/evidence.py compare SAVED.json 'REF' --brain PATH
```

Only this compact comparison reaches the model. `state` is `changed`, `unchanged` or `unknown`; `content_changed` compares stored content even when freshness is unknown.

- A new `observed` time alone is not a change, nor is a record `time` stated with another time zone's offset; every other record field is.
- A partial baseline or record, an incomplete read or a source that is not both `active` and `fresh` makes the state `unknown`. A missing read fails; it never means unchanged.
- For a record, `unchanged` means no newer local revision: an older item can change upstream unseen by a window sensor.

## Revise a belief

Keep the current conclusion in the owning project or concept. When the old claim matters, keep its decision note and capture and write a successor decision note linking what it supersedes and why. Retire a note with `status: deprecated`; say "disputed" in the text when evidence conflicts, since newer evidence is not automatically more authoritative.

Declare the relations you use in `bf.yaml` ([links](links.md)), then write `[evidence](bf://NAME/concepts/policy.md?rel=depends-on#retention)` or `[previous decision](bf://NAME/actions/YYYY-MM-DD_topic/outputs/decision.md?rel=supersedes)`: generic links establish neither dependency nor supersession, and BF never chooses the winning claim.

## Review impact lightly

When selected evidence changes, list its dependents with `bf read 'REF' --rel depends-on`, following `next_offset`, and read each one's `depends-on` `claims`: `target` tells whether it depends on the changed section, `origin` which section makes the claim. Review at most **two hops, ten distinct dependents, one visit per qualified ref**, which also stops cycles, and check the declared meaning of equal relation names across brains.

For each affected conclusion, report the dependency path and whether it needs review, remains justified or lacks evidence. Change alone never proves falsity, and generic or `supersedes` links propagate no impact. An unfollowed `next_offset`, truncated claims, a missing brain or an exhausted limit makes the review explicitly incomplete. Reuse a previous review while the evidence digest and conclusion are unchanged.
