# Identities, tags and relationships

Use when changing classification, stable refs or explicit graph relationships. The [link reference](https://fmind.github.io/brain-framework/docs/link-reference/) owns the exact rules.

## Author a relationship

Use stable `bf://<bf.yaml name>/...` addresses across brains. Only projects, concepts and `ACTION.md` notes declare `entity`, `aliases` and `tags`. Other Markdown, such as action inputs and outputs, is ordinary: BF ignores those fields there, and its typed links use the file as their subject.

An OKF note may declare `entity: bf://NAME/people/ID` (or another logical namespace), with verified namespaced identities (`scheme:value`) in `aliases`, never display names. BF entities and aliases must use this brain's own namespace; link to a foreign brain instead of claiming its identity. A record ref such as `jira:PROJ-1` is already an identity: link to it rather than repeating it as an alias, which makes both owners ambiguous.

Declare each role under the existing `schema:` mapping in `bf.yaml`, with a `description`, `type: identity` and `relation: true`, before writing `[label](bf://NAME/path?rel=ROLE#section)`. `bf init` already declares `author`, `owner`, `depends-on` and `related-to`; `tagged-with` (tag membership) and `links` (untyped links on role pages) are reserved, and so is `cites`. The subject is the note entity, otherwise its file: to state another entity's relationship, write the link in that entity's note. `rel` is the only BF link query and precedes the fragment. Keep authorship and ownership in named relationships, not URI userinfo. Other URI schemes retain their original query semantics.

Two optional settings refine a declared role. `broader: PARENT` makes it a narrower kind of another declared role without its own `broader`, one level deep: `--rel PARENT` then also lists this role's items, while backlink groups stay by actual role. `targets: [PREFIX, ...]` restricts the identities the role may point to, such as `["repo:"]` (quote prefixes in YAML): collection fails the run without changing evidence when a mapped value falls outside them, and `bf validate` reports typed note links and stored records that do. An OKF note's `sources` entries become typed `cites` claims, so evidence a note derives from stays distinct from a plain link: `bf read REF --rel cites` lists what cites an item.

Relative links start from the file containing them. In `projects/` and `concepts/`, a leading `/` starts from that folder (OKF bundle-relative), so `[Tables](/tables.md)` in a concept names `concepts/tables.md`; `ACTION.md` notes and attachments need ordinary relative links. Embedded images are checked like links. Page addresses are valid link targets when `bf read` can open them: home, `tasks`, `tags`, a tag some note declares, an existing folder under `projects/`, `concepts/` or `actions/`, a valid period, and under `memories/` a known source with a period, `undated` or an existing record file. Pages own their namespace: an entity or alias cannot name one.

`bf read IDENTITY` returns incoming links grouped by relationship under `backlinks`, each group previewing its 5 newest items, and the claims with that explicit subject under `claims`, each with its `origin` (the exact section or record making it) and that origin's `time`. `bf read IDENTITY --rel ROLE` lists a whole group, and `bf search 'IDENTITY'` gives each incoming link's `relations` with their `origin`. Exact reads accept BF addresses. Use explicit heading anchors (`## Display title {#stable-id}`) when a section needs a durable ref. Keep the brain's `name` stable across clones. Selected-brain scope is a boundary: links never add another brain or contact a network. Ambiguous aliases and incomplete searches need review; `bf validate` reports foreign links under `unresolved` without opening them.

Related brains belong in `bf.yaml` as `brains: {team: {path: ../team}}`. Use stable matching names and paths relative to the declaring root. Search/read include direct references only; check `problems` before claiming absence. References and registration never grant sensor execution permission.

## Reuse topic labels

Browse `bf read tags` before adding frontmatter such as `tags: [agents, retrieval]`. Prefer established lowercase hyphenated names and avoid parallel spellings. Tags are exact, case-sensitive and brain-local; they classify authored notes, while concepts explain knowledge and typed links declare roles. Linking to `bf://NAME/tags/LABEL` does not tag a note. Never infer classification from provider labels or edit collected records to tag them.

After an edit, follow the returned `bf://NAME/tags/LABEL` ref and search within that exact scope. Preserve `type` and lifecycle `status`; neither is a work-progress field.

Shared field meanings, types, cardinality and examples live under `schema` in `bf.yaml`; sensor `fields` map explicit output paths or constants into them. Never infer identity or relationships from name similarity.
