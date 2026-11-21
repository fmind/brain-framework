---
type: guide
status: stable
updated: 2026-11-15
tags: [getting-started]
summary: Starter concept showing YAML frontmatter, Python-Markdown attr_list metadata, unified links, and pure JSON memories.
---

# Welcome

YAML configures the brain (`bf.yaml`), Markdown stays readable for humans (`projects/`, `concepts/`, `actions/`), and `bf` compiles notes and sensor outputs into individual JSON files under `memories/` for agents. {: #principles role=principle}

## Conventions {#conventions .core format=okf}

Attach metadata to headings, links, or blocks with Python-Markdown `attr_list` syntax, link uniformly across prose and frontmatter using relative paths like [Conventions](#conventions){rel=self}, `<source>:<id>`, or `bf://` URIs (`bf:///concepts/welcome.md`), and run `bf check` after edits.
