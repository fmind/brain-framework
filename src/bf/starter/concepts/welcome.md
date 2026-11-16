---
type: guide
status: stable
updated: 2026-11-15
tags: [getting-started]
summary: Starter concept showing YAML frontmatter, Python-Markdown attr_list metadata, bf:// links, and pure JSON memories.
---

# Welcome

YAML configures the brain (`bf.yaml`), Markdown stays readable for humans (`projects/`, `concepts/`, `actions/`), and `bf` compiles notes and sensor outputs into individual JSON files under `memories/` for agents. {: #principles role=principle}

## Conventions {#conventions .core format=okf}

Attach metadata to headings, links, or blocks with Python-Markdown `attr_list` syntax, use relative paths in Markdown prose such as [Conventions](#conventions){rel=self} (or `../memories/<source>/<sha256>.json` for sensor records) and standard `bf://` URLs (`bf:///concepts/welcome.md`) in frontmatter/JSON links, and run `bf build && bf validate && bf eval` after edits.
