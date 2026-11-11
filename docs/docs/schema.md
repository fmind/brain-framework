---
icon: lucide/braces
description: Exact formats of bf.yaml, notes, records, shared fields and sensor mappings, with editor validation.
---

# Notes, records and fields

This reference defines the persisted formats BF reads: `bf.yaml`, OKF notes, record JSON files and ontology field mappings. See the [link reference](link-reference.md) for address syntax.

```yaml
# https://fmind.github.io/brain-framework/
version: 8
name: brain
```

`version: 8` names the persisted brain format ([versioning](upgrades.md#versioning-and-stability)). Every program's `command` must start with either a bare executable name on `PATH` or a normalized relative path under `sensors/`, `routines/` or `channels/` (no absolute paths, `..` or placeholders in `command[0]`).

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
- **Declared `fields`**: validated against `bf.yaml` `fields:` (invalid values fail `bf validate` without hiding the note from search; relations placed at top level instead of under `fields:` are flagged).
- **Special files**: folder-root `index.md` (allows `okf_version`) and changelog `log.md` (`YYYY-MM-DD` headings without frontmatter) carry no review reminders or tasks.
- **Ordinary Markdown** (such as `actions/*/inputs/*.md`): indexes `title`, `type`, `status`, `updated`, `summary` and `description` when valid, and ignores `entity`, `aliases`, `resource`, `tags`, `fields`, `sources` and `stale_after` so imported files never claim identities.
- **Conflict markers**: unindented `<<<<<<<`, `|||||||`, `>>>>>>>` lines make any Markdown file invalid and unsearchable until resolved.

## Record revisions and provenance

Each source stores one JSON file per record at `memories/SOURCE/<sha256-of-id>.json`:

| Field                 | Meaning                                                                                     |
| --------------------- | ------------------------------------------------------------------------------------------- |
| `time`                | Event timestamp (timezone-aware ISO 8601).                                                  |
| `attributes.updated`  | Upstream modification timestamp (prevents older revisions from overwriting newer ones).     |
| `attributes.observed` | Set by BF when this revision was first collected (preserved while content stays identical). |
| `attributes.partial`  | `true` when the sensor or `max_text_bytes` deliberately kept truncated text.                |

`updated`, `observed` and `partial` are reserved keys under `attributes`. Files under `memories/` whose filename does not match `sha256(id)` are reported by `bf validate` and skipped by search and collection.

## Ontology: shared fields and relations {#shared-fields-and-sensor-mappings}

Declare shared fields under `fields:` in `bf.yaml` and map sensor attributes into them via JSON Pointers (`path`) or constants (`value`):

```yaml
# https://fmind.github.io/brain-framework/
version: 8
name: brain
fields:
  author:
    description: Account explicitly credited as the author.
    type: identity
    cardinality: many
    relation: true
    examples: [[person:email/alice@example.test]]
  kind:
    description: Common kind of source item.
    type: string
    cardinality: one
    examples: [commit, issue, pull-request, message, document]
sensors:
  git-commits:
    command: [sensors/git-history.py, "{{home}}/code", "{{start}}", "{{end}}"]
    fields:
      author: { path: /attributes/author_refs }
      kind: { value: commit }
```

### Standard vocabulary

`bf init` declares these fields with default JSON Pointer `path` mappings so sensors emitting standard attribute names work with zero per-sensor `fields:` boilerplate:

| Field         | Type, cardinality        | Default `path`                 | Meaning                                                                          |
| ------------- | ------------------------ | ------------------------------ | -------------------------------------------------------------------------------- |
| `author`      | identity, many, relation | `/attributes/author_refs`      | Credited author.                                                                 |
| `owner`       | identity, many, relation | `/attributes/owner_refs`       | Responsible account or organization.                                             |
| `assignee`    | identity, many, relation | `/attributes/assignee_refs`    | Assigned account.                                                                |
| `participant` | identity, many, relation | `/attributes/participant_refs` | Any participant (broader than `sender`/`recipient`/`cc`/`organizer`/`attendee`). |
| `sender`      | identity, many, relation | `/attributes/sender_refs`      | Message sender (`broader: participant`).                                         |
| `recipient`   | identity, many, relation | `/attributes/recipient_refs`   | Direct recipients (`broader: participant`).                                      |
| `cc`          | identity, many, relation | `/attributes/cc_refs`          | Copied accounts (`broader: participant`).                                        |
| `organizer`   | identity, many, relation | `/attributes/organizer_refs`   | Calendar organizer (`broader: participant`).                                     |
| `attendee`    | identity, many, relation | `/attributes/attendee_refs`    | Listed calendar attendees (`broader: participant`).                              |
| `repository`  | identity, many, relation | `/attributes/repository_refs`  | Owning repository (`repo:HOST/OWNER/NAME`).                                      |
| `project`     | identity, many, relation | `/attributes/project_refs`     | Provider project identity.                                                       |
| `part-of`     | identity, many, relation | `/attributes/parent_refs`      | Parent thread, folder, epic or container.                                        |
| `label`       | identity, many, relation | `/attributes/label_refs`       | Provider label (`label:github.com/OWNER/REPO/NAME`, `gmail-label:ID`).           |
| `kind`        | string, one              | `/attributes/kind`             | Singular item kind (`commit`, `issue`, `pull-request`, `document`).              |
| `status`      | string, one              | `/attributes/status`           | Upstream tool status.                                                            |

`bf init` also declares note relations `depends-on`, `related-to` and `supersedes` (without default sensor paths). Standard identity conventions: lowercase `person:email/ADDRESS`, `person:HOST/LOGIN`, lowercase `repo:HOST/OWNER/NAME`, percent-encoded `label:github.com/OWNER/REPO/NAME`, and `gmail-label:ID` / `gmail-thread:ID`.

### Types and cardinality

- **`type`**: `string` (nonempty, <= 8,192 chars, no control chars), `integer`, `number`, `boolean`, `timestamp` (timezone-aware) or `identity` (exact `scheme:value` per [identity rules](link-reference.md#identity-rules)). `tagged-with`, `links` and `cites` are reserved field names.
- **`cardinality`**: `one` (required when mapped explicitly on a sensor; omitted when a field-level default `path` is absent), `optional` (default; omitted on missing/`null`) or `many` (deduplicated list of <= 1,000 values).
- **`relation: true`**: requires `type: identity`; creates graph edges from the record/note to each target value.

### Narrower relations and allowed targets

- **`broader`**: names one top-level declared relation (`bf read TARGET --rel participant` includes `organizer` and `attendee` edges at read time without duplicating stored values).
- **`targets`**: 1–64 allowed `scheme:` / `scheme:prefix` strings (such as `["person:email/"]`). Out-of-range values fail collection before saving (`record N: field organizer: identity outside the declared targets`) and fail `bf validate`.

### Mapping rules

Sensors output a JSON array, JSON Lines (one JSON record object per line) or `{records, incomplete}` ([partial coverage](sensors.md#report-partial-coverage)). Each input record accepts only:

- **Required**: `id` (1–4,096 chars, <= 7,988 percent-encoded, no control chars), `title` (<= 4,096 chars).
- **Optional**: `text` (<= 4 MiB unless `max_text_bytes` truncates it and sets `attributes.partial: true`), `time` (ISO 8601 with timezone), `url`, `links` (identities/URLs), `aliases` (namespaced identities), `attributes` (JSON object; no sensor-supplied `fields`).

JSON Pointers escape `/` as `~1` and `~` as `~0`. Any invalid record aborts the entire collection before writing.

### Reproject stored records

Re-apply updated `fields:` declarations and sensor mappings to already-stored records without re-running the sensor:

```bash
bf build --reproject git-commits --dry-run
bf build --reproject git-commits
```

Reprojection preserves `observed` and all raw record attributes, commits in batches of up to 1,000 records, and exits 1 listing up to 200 `problems` if stored records violate the new field rules.
