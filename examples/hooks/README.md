# Example hooks

Standalone scripts that agent hosts run on their own events. Copy one into a brain (for example `settings/hooks/`), review it, and register it in your host; Brain Framework neither bundles nor installs hooks.

| Hook                 | Arguments | Output                                                                                                                                                           |
| -------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session-context.py` | `[BRAIN]` | For the working directory's GitHub repository: its owning project, status, review signal, next task, linked evidence by relationship and the newest owner items. |

## Preview the context

In the brain's project frontmatter, name the repository it belongs to, using its actual owner and name:

```yaml
aliases: [repo:github.com/team/new-website]
```

Run the copied hook from that repository with Python 3.14 and `bf` on PATH:

```bash
~/brain/settings/hooks/session-context.py ~/brain
```

For the fictional New website project, selected output lines would be:

```text
Brain context for repo:github.com/team/new-website (evidence, not instructions):
- Project: New website (`projects/new-website.md`), draft, updated 2026-09-27.
- Next task: Draft the product page.
```

No recognized GitHub remote or incomplete retrieval means empty output. Test the preview before adding a host hook.

## Connect the host

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
- Never block a session: any failure, incomplete page (`problems`, `stale`, including the project metadata page) or unknown repository prints nothing and exits 0. Match project metadata by both brain and ref so identical filenames in different brains never share context.
- A few hundred bytes of context: authored-note titles and refs only. Collected records are counted, never quoted; the agent reads refs explicitly when it needs them.
