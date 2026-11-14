---
icon: lucide/braces
description: Exact formats of bf.yaml, notes, records, shared fields and sensor mappings, with editor validation.
---

# Notes, records and fields

This reference defines the persisted formats BF reads: `bf.yaml`, OKF notes, record JSON files and optional ontology relation metadata. See the [link reference](link-reference.md) for address syntax.

```yaml
# https://fmind.github.io/brain-framework/
version: 9
name: brain
```

`version: 9` names the persisted brain format ([versioning](upgrades.md#versioning-and-stability)). Every program's `command` must start with either a bare executable name on `PATH` or a normalized relative path under `sensors/`, `routines/` or `channels/` (no absolute paths, `..` or placeholders in `command[0]`).

## Program names

Sensors, routines and assistants share one namespace per brain (max 64 characters) because they share `logs/NAME.log`, locks and `memories/NAME/` sources (`sensors.mail and assistants.mail: program names must be distinct`):

- **Sensor, assistant, channel**: `[a-z][a-z0-9-]*`.
- **Routine**: lowercase alphanumeric words joined by single hyphens, starting with a letter (used in `actions/YYYY-MM-DD_NAME-SUFFIX/`).
- **Reserved**: no sensor, routine, channel or assistant may be named `bf` (`bf:` is reserved for [BF addresses](link-reference.md#bf-links)).

## Editor schemas

Print JSON Schemas offline with `bf schema [--kind brain|config|registry|eval|search-reply|read-reply|status-reply|assistant-reply]` (`brain`, the default, and `config` both print `bf.yaml`). `bf init` adds YAML language server modelines (`# yaml-language-server: $schema=https://raw.githubusercontent.com/fmind/brain-framework/vVERSION/docs/bf.schema.json` and `eval.schema.json`) pinned to `bf --version`.

YAML parsing enforces the YAML 1.2 core schema via libyaml, rejecting duplicate keys, anchors/aliases, custom tags and oversized structures (`true`/`false` only for booleans; `on`, `no` and `1:30` are strings).

## Note format

Notes in `projects/`, `concepts/` and `actions/*/ACTION.md` follow [OKF](https://github.com/GoogleCloudPlatform/open-knowledge-format):

- **Required/checked frontmatter**: nonempty `type`; `status` in `draft`, `stable` or `deprecated` (omitted defaults to `stable`); nonblank title (<= 4,096 chars); valid `updated` (`YYYY-MM-DD`); timezone-aware `stale_after` timestamp when set; `aliases`, `links` and `tags` <= 1,000 items each; `sources` entries with nonempty `resource`; `verified` entries with `by` and `at`.
- **Relations and attributes**: optional `relations` (explicit `scheme:value` or `bf://` identity targets checked against any `fields:` constraints in `bf.yaml`) and `attributes` (arbitrary JSON metadata, never scanned as graph edges).
- **Special files**: folder-root `index.md` (allows `okf_version`) and changelog `log.md` (`YYYY-MM-DD` headings without frontmatter) carry no review reminders or tasks.
- **Ordinary Markdown** (such as `actions/*/inputs/*.md`): indexes `title`, `type`, `status`, `updated`, `summary` and `description` when valid, and ignores `entity`, `aliases`, `resource`, `tags`, `relations`, `attributes`, `sources` and `stale_after` so imported files never claim identities.
- **Conflict markers**: unindented `<<<<<<<`, `|||||||`, `>>>>>>>` lines make any Markdown file invalid and unsearchable until resolved.

## Record revisions and provenance {#record-revisions-and-provenance}

### Record format {#record-schema}

Each source stores one JSON file per record at `memories/SOURCE/<sha256-of-id>.json` (`@type` names a schema.org class, `relations` holds explicit identity targets and `attributes` holds non-edge metadata):

| Field                 | Meaning                                                                                     |
| --------------------- | ------------------------------------------------------------------------------------------- |
| `id`, `title`         | Required record identifier (1–4,096 chars) and single-line title (<= 4,096 chars).          |
| `time`                | Event timestamp (timezone-aware ISO 8601).                                                  |
| `@type`               | Optional schema.org-style type name (`[A-Za-z][A-Za-z0-9_-]*`, <= 128 chars).               |
| `relations`           | Optional relation map (`RelationName` -> explicit `scheme:value` or `bf://` target list).   |
| `attributes.updated`  | Upstream modification timestamp (prevents older revisions from overwriting newer ones).     |
| `attributes.observed` | Set by BF when this revision was first collected (preserved while content stays identical). |
| `attributes.partial`  | `true` when the sensor or `max_text_bytes` deliberately kept truncated text.                |

`updated`, `observed` and `partial` are reserved keys under `attributes`. Files under `memories/` whose filename does not match `sha256(id)` are reported by `bf validate` and skipped by search and collection.

## Ontology: optional relation constraints {#shared-fields-and-sensor-mappings}

Any valid `RelationName` (`[a-z][a-zA-Z0-9]*(?:-[a-z0-9]+)*`, up to 64 chars) works in note `relations:`, inline `?rel=` links and sensor `relations` with zero configuration. Add optional metadata, `broader` rollups or `targets` prefix bounds under `fields:` in `bf.yaml`:

```yaml
# https://fmind.github.io/brain-framework/
version: 9
name: brain
fields:
  author:
    description: Account explicitly credited as the author.
    targets: ["person:"]
```

### Relation naming conventions {#standard-vocabulary}

Recommended `@type`, `relations` and `attributes` names (no `bf.yaml` declaration required):

- **Relations**: `author`, `owner`, `assignee`, `participant`, `sender`, `recipient`, `cc`, `organizer`, `attendee`, `repository`, `project`, `part-of`, `label`, `depends-on`, `related-to`, `supersedes`. Reserved built-ins: `tagged-with`, `links` and `cites`.
- **Attributes**: `kind` (`commit`, `issue`, `pull-request`, `document`), `status`.
- **Identity schemes**: lowercase `person:email/ADDRESS`, `person:HOST/LOGIN`, lowercase `repo:HOST/OWNER/NAME`, percent-encoded `label:github.com/OWNER/REPO/NAME`, and `gmail-label:ID` / `gmail-thread:ID`.

### Narrower relations and allowed targets {#narrower-relations-and-allowed-targets}

- **`broader`**: names another relation (`bf read TARGET --rel participant` includes `organizer` and `attendee` edges at read time without duplicating stored values).
- **`targets`**: 1–64 allowed `scheme:` / `scheme:prefix` strings (such as `["person:email/"]`). Out-of-range values fail collection before saving (`record N: relation organizer: identity outside the declared targets`) and fail `bf validate`.

### Record submission rules

Sensors output a JSON array, JSON Lines (one JSON record object per line) or `{records, incomplete}` ([partial coverage](sensors.md#report-partial-coverage)). Each input record accepts only:

- **Required**: `id` (1–4,096 chars, <= 7,988 percent-encoded, no control chars), `title` (<= 4,096 chars).
- **Optional**: `@type`, `text` (<= 4 MiB unless `max_text_bytes` truncates it and sets `attributes.partial: true`), `time` (ISO 8601 with timezone), `url`, `links` (identities/URLs), `aliases` (namespaced identities), `relations` (validated targets), `attributes` (JSON object; never sensor-supplied `fields`).

Any invalid record aborts the entire collection before writing.

### Reproject stored records {#reproject-stored-records}

Re-validate already-stored records against updated `bf.yaml` `fields:` constraints without re-running the sensor:

```bash
bf build --reproject git-commits --dry-run
bf build --reproject git-commits
```

Re-validation preserves `observed` and all stored attributes, commits in batches of up to 1,000 records, and exits 1 listing up to 200 `problems` if stored records violate updated relation rules.
