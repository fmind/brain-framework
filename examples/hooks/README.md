# Example hooks

Standalone scripts that agent hosts run on their own events. Copy one into a brain (for example `settings/hooks/`), review it, and register it in your host; Brain Framework neither bundles nor installs hooks.

| Hook                 | Arguments | Output                                                                                                                                                           |
| -------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session-context.py` | `[BRAIN]` | For the working directory's GitHub repository: its owning project, status, review signal, next task, linked evidence by relationship and the newest owner items. |

Claude Code adds a `SessionStart` hook's standard output to the session context. In `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "~/brain/settings/hooks/session-context.py ~/brain" }] }
    ]
  }
}
```

Hosts without hooks can run the same script from their session instructions. Start a session in a repository that a project note names in its `aliases` (`repo:github.com/owner/name`) and check that the context appears; `bf status` then counts its reads under `usage`.

## Contract

`tests/test_adapters_hooks.py` checks every example with fake `git` and `bf` executables.

- Python 3.14 standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Read-only and offline: `git remote get-url origin` and `bf read` with literal argv, bounded time and output.
- Never block a session: any failure, incomplete page (`problems`, `stale`) or unknown repository prints nothing and exits 0.
- A few hundred bytes of context: owner-written titles and refs only. External items are counted, never quoted; the agent reads refs explicitly when it needs them.
