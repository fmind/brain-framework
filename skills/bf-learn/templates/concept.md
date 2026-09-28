---
type: concept
title: Concept title
description: A concise description of the knowledge this page preserves.
status: draft
---

# Concept title

State the useful knowledge and its limits. Add `sources` mappings with a `resource` (a URL or a record ref such as `meetings:decision-1`) for supporting evidence. Do not invent provenance or verification to fill a template.

## Related knowledge

Use ordinary relative links for navigation, such as `[Other](/other.md)` for `concepts/other.md`, or a declared typed BF link for a relationship: `[Related](bf://BRAIN/concepts/ID?rel=related-to)`. Replace placeholders with verified identities. `bf init` declares `related-to`; declare any other role in `bf.yaml` first. Use `entity` only when this note explicitly owns that identity, and `aliases` only for namespaced identities such as `repo:github.com/owner/name` (lowercase, as the example sensors write it).
