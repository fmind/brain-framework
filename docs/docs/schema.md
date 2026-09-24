# Configuration schema

The [generated JSON Schema](../fkf.schema.json) describes `fkf.yaml`. It comes from the same strict models the loader uses.

```bash
fkf schema
```

Contributors regenerate the checked-in schema from the FKF checkout with `mise run generate:schema`.

`fkf.yaml` holds `version: 2`, the base `name` and optional [collectors](sources.md). Unknown keys, duplicate keys, anchors and aliases are rejected. Machine-specific choices, such as which bases this user searches and which may collect here, belong in `~/.config/fkf/config.yaml`; see [personal and team bases](base.md#personal-and-team-bases).
