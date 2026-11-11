# Session handoff

Use before handing an open `draft` action to another session or running `/compact`. Sync durable decisions to the owning project first; never create standalone handoff files or paste transcripts.

## Check and resume

```bash
python3 SKILL_DIR/scripts/check-handoff.py 'actions/2026-09-27_website-review/ACTION.md' --brain ~/brain
```

`check-handoff.py` reads `#context` and `#resume` offline via the installed `bf read` and returns `checked`, `passed` and per-section `ref`, `words`, `bytes` and `limits` (exit 0 pass, 1 budget/read failure, 2 invalid input). Verify accuracy, freshness and the six-ref limit yourself, then pass the returned refs to the next session as evidence.

## Claude Code compaction

Refresh the action and run `check-handoff.py` before `/compact`. For an optional post-compaction check, merge a temporary `SessionStart` (`compact|resume`) hook into `.claude/settings.local.json` and remove it once the action settles:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "compact|resume",
        "hooks": [
          {
            "type": "command",
            "command": "python3 SKILL_DIR/scripts/check-handoff.py actions/2026-09-27_website-review/ACTION.md --brain \"$HOME/brain\" --hook",
            "timeout": 45
          }
        ]
      }
    ]
  }
}
```

`--hook` emits refs and counts (never section bodies) as a `SessionStart` evidence payload and exits 0 even when over budget.
