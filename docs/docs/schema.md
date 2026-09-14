# Configuration schema

The [generated JSON Schema](../fkf.schema.json) describes fkf.yaml. It is generated from the same strict models used by the loader.

```bash
fkf schema
mise run generate:schema
```

The configuration is intentionally small: version, persistent base id, name, and optional source commands. Unknown keys, duplicate YAML keys, anchors and aliases are rejected. A machine-local overlay may override source settings only. See [base configuration](base.md) and [source execution](sources.md).
