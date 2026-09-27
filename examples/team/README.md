# Two contributors, one brain

Run from the framework checkout:

```bash
uv run python examples/team/demo.py
```

The example creates and removes a disposable Git repository containing fictional evidence. It uses no providers or remote repositories. Two contributors add separate records and independently named action sessions, then merge. Two further contributors change the same record incompatibly.

Expected result:

```json
{ "independent_merge": "clean", "records": 3, "actions": 2, "same_record_merge": "conflict", "conflict_rejected": true }
```

Each record has one stable source/ID-derived JSON filename. Actions use `YYYY-MM-DD_topic-UUID/ACTION.md`, with a fresh UUID for every independent session. Only competing revisions require judgment; see the [team guide](../../docs/docs/team.md) and [resolution procedure](../../skills/bf-maintain/references/conflicts.md).
