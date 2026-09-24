# Configuration schema

The [generated JSON Schema](../bf.schema.json) describes `bf.yaml`. It comes from the same strict models the loader uses.

```bash
bf schema
```

Contributors regenerate the checked-in schema from the Brain Framework checkout with `mise run generate:schema`.

`bf.yaml` holds `version: 3`, the brain `name` and optional [sensors](sensors.md). Unknown keys, duplicate keys, anchors and aliases are rejected. Machine-specific choices, such as which brains this user searches and which may collect here, belong in `~/.config/bf/config.yaml`; see [personal and team brains](brain.md#personal-and-team-brains).
