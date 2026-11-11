# Evidence, revisions, impact and sharing

Use when a decision must remain auditable after sources change, when checking dependents or when preparing knowledge for another audience.

Treat `"generated":true` records (and records carrying assistant `trigger`, `job` and `window` attributes) as model output: read and cite their underlying sources, never the generated summary. For highlight records, separate `text` (quoted passage) and `attributes.source_date` from `attributes.annotation` and capture `time`.

## Retain a revision {#retain-a-revision}

Capture only the specific note section or record a consequential decision relies on:

- Store captures of Git-ignored sources (`memories/`) in `originals/` and cite the path in a code span; store versioned captures in the owning action's `inputs/` or `assets/`.
- `evidence.py` runs offline (up to 19 MiB), writes owner-only files without overwriting, and refuses listing pages, lone text slices and incomplete or `stale` reads:

```bash
python3 SKILL_DIR/scripts/evidence.py capture 'REF' --brain PATH --output NEW.json
python3 SKILL_DIR/scripts/evidence.py compare SAVED.json 'REF' --brain PATH
```

`compare` returns `state` (`changed`, `unchanged`, `unknown`) and `content_changed`: `observed` or timezone-offset differences alone are unchanged, while partial content or a source not both `active` and `fresh` (or marked `failed` or `degraded`) yields `unknown`.

## Revise a belief and check impact

- Update the current conclusion in its owning note; when the old rationale matters, write a successor note with `[prior](bf://NAME/actions/YYYY-MM-DD_topic/outputs/decision.md?rel=supersedes)` or retire obsolete notes with `status: deprecated` (mark unresolved conflicts as disputed).
- When evidence changes, inspect `bf read 'REF' --rel depends-on` (following `next_offset`) and each dependent's `claims` (`target` and `origin`), bounded to **two hops, ten distinct dependents and one visit per ref**. Report affected conclusions, unaffected ones and any unfollowed pages or missing brains.

## Share knowledge with another audience {#share-across-audiences}

Federation (`brains:`) never authorizes publishing. Stage at most five selected notes in the action's `outputs/` or a private task folder (keeping source-to-destination mappings and private captures outside the candidate):

1. Strip private passages, identifiers, URLs, aliases and backlinks; label unsupported claims as attributed assessments or omit them.
1. Rewrite `bf://` links and compatible `fields:` relations for the destination brain, dropping source `entity`/`aliases`/`resource`, `brains:`, programs and registrations.
1. Run `bf validate` and `bf eval` in a disposable destination brain **without source-brain references**, and inspect unresolved links manually before asking to publish.
