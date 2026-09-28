---
description: Collect exact passages with source locations and separate annotations from a local export.
---

# Collect selected highlights

Keep an exact passage, its source location and your separate annotation as a searchable record. Start with an initialized brain and work inside it. This guide uses a fictional local export; it does not need provider credentials.

## Prepare the export

Use the reviewed [highlights sensor](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.py) when a selected passage is more useful than importing a whole document. Copy it and the [fictional export](https://github.com/fmind/brain-framework/blob/main/examples/sensors/highlights.json) from the release matching your installation, then review the script:

```bash
mkdir -p sensors inputs
examples="https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/sensors"
curl -fsSLo sensors/highlights.py "$examples/highlights.py"
curl -fsSLo inputs/highlights.json "$examples/highlights.json"
```

The export holds one highlight in an explicit portable format: convert exports from reading tools into it before collection. The sensor does not parse a Readwise export directly or fetch the original document.

```json
{
  "version": 1,
  "highlights": [{
    "id": "brief-clarity",
    "title": "New website brief",
    "selection": "Visitors need a clear product explanation before signing up.",
    "annotation": "Review whether our first page explains who the product helps.",
    "source_url": "https://example.com/website-brief",
    "locator": { "page": 2, "section": "Audience" },
    "captured_at": "2026-09-27T12:00:00Z",
    "source_date": "2026-09-25"
  }]
}
```

All fields are required except `annotation` and `source_date`. These are supplied provenance, not independently verified facts. Unknown fields and duplicate JSON keys fail.

<details markdown="1">
<summary>Export field rules and limits</summary>

| Field         | Rule                                                                                                     |
| ------------- | -------------------------------------------------------------------------------------------------------- |
| `id`          | Stable across edits; 1–128 letters, digits, dots, underscores or hyphens; starts with a letter or digit. |
| `title`       | Nonempty, single-line document title; at most 4096 UTF-8 bytes.                                          |
| `selection`   | Exact selected text; at most 64 KiB.                                                                     |
| `annotation`  | Your separate interpretation; optional, may be empty, at most 64 KiB.                                    |
| `source_url`  | HTTPS document URL without credentials; at most 8192 bytes.                                              |
| `locator`     | Positive `page` up to 1000000, nonempty `section` up to 4096 bytes, or both.                             |
| `captured_at` | Timestamp with seconds and a timezone.                                                                   |
| `source_date` | Optional document date in `YYYY-MM-DD`.                                                                  |

</details>

## Configure collection

Add these entries to the `sensors:` and `schema:` mappings, creating either mapping if absent and preserving existing entries:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  highlights:
    command: [
      uv,
      run,
      --no-project,
      --python,
      "3.14",
      sensors/highlights.py,
      reading,
      "{{brain}}/inputs/highlights.json",
    ]
    mode: snapshot
    refresh: 0
    fields:
      source-document: { path: /url }
schema:
  source-document:
    description: The document containing this selected passage.
    type: identity
    cardinality: one
    relation: true
```

`reading` is the stable export scope label; changing it changes every record id. Use a lowercase label starting with a letter, with only letters, digits or hyphens, up to 64 characters. The sensor reads exactly one regular UTF-8 file, refuses symlinks in the file or its ancestors, and bounds input to 8 MiB and 1000 highlights. It emits at most 16 MiB. Every item is checked before output, so malformed or oversized exports fail without partial records. No source text is truncated and no source URL is contacted.

## Collect and inspect

```bash
bf collect highlights --dry-run
bf collect highlights
bf search "product explanation" --scope memories/highlights
bf read highlights:reading/brief-clarity
bf validate
```

The preview runs the local script without saving records; the next collection stores one record for the sample export.

The exact read returns the selection as `record.text`, the parent document as `record.url` and `record.fields.source-document`, and the page/section as `record.attributes.locator`. The capture timestamp becomes `record.time`; `record.attributes.source_date` remains distinct. `record.attributes.annotation` holds the interpretation and is not indexed as source text. Two highlights of the same document keep distinct ids. Link this record from the project decision, then read the passage and location before adopting the annotation as a conclusion.

## Update or recover

Reimporting the same ids updates their records. Snapshot mode removes ids absent from a successful nonempty replacement, so always supply the complete selected catalog for this label. An empty export cannot erase an existing catalog. One that would remove more than half of it and more than 10 records fails unless you accept that [removal](sensors.md#define-the-scope-before-adding-a-sensor) with `bf collect highlights --allow-removal`. A malformed export preserves all previous records. A fresh import proves the selected local export was read, not that the source is current; retain an [evidence capture](agents.md#decision-workflows) before replacing a revision needed to explain a decision.

Next: [connect the passage to a decision](links.md) or [retain a revision for comparison](agents.md#retain-and-compare-evidence).
