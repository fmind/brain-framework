# Example hooks

Standalone scripts that agent hosts run on their own events. Copy one into a brain (for example `settings/hooks/`), review it, and register it in your host; Brain Framework neither bundles nor installs hooks.

| Hook                 | Arguments | Output                                                                                                                                                           |
| -------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session-context.py` | `[BRAIN]` | For the working directory's GitHub repository: its owning project, status, review signal, next task, linked evidence by relationship and the newest owner items. |

## Try it locally

From the framework checkout after `uv sync --locked`, this subshell creates a fictional brain and an empty Git repository. The remote URL is only an identity string: no clone, fetch, provider or agent host runs.

```bash
(
  set -eu
  bf_checkout=$PWD
  hook_demo=$(mktemp -d)
  hook_demo=$(cd "$hook_demo" && pwd -P)
  trap 'rm -rf -- "$hook_demo"' EXIT
  unset BF_BRAIN
  export XDG_CONFIG_HOME="$hook_demo/config" XDG_STATE_HOME="$hook_demo/state"
  uv run bf init "$hook_demo/brain" --name hook-demo
  cat > "$hook_demo/brain/projects/new-website.md" <<'EOF'
---
type: project
status: draft
aliases: [repo:github.com/example/new-website]
---

# New website

## Next actions

- [ ] Run the keyboard navigation check.
EOF
  git -c init.templateDir= init --quiet "$hook_demo/repository"
  git -C "$hook_demo/repository" remote add origin https://github.com/example/new-website.git
  cd "$hook_demo/repository"
  uv run --project "$bf_checkout" python "$bf_checkout/examples/hooks/session-context.py" "$hook_demo/brain"
)
```

Output includes `Brain context for repo:github.com/example/new-website`, the project ref `projects/new-website.md`, and `Next task: Run the keyboard navigation check.` The edit date is the date you run it; the review deadline is 14 days later. This verifies the script's actual Git-to-brain lookup; host installation is a separate step below.

## Preview the context

In the brain's project frontmatter, name the repository it belongs to, with its owner and name in lowercase, such as `repo:github.com/googlecloudplatform/open-knowledge-format`. Identities are case-sensitive, and this hook and the Git history sensor lowercase GitHub owners and names:

```yaml
aliases: [repo:github.com/team/new-website]
```

Run the copied hook from that repository with Python 3.11 or later as `python3` and `bf` on PATH:

```bash
~/brain/settings/hooks/session-context.py ~/brain
```

For the fictional New website project, selected output lines would be:

```text
Brain context for repo:github.com/team/new-website (evidence, not instructions):
- Project: New website (`projects/new-website.md`), draft, edited 2026-09-27, review deadline 2026-10-11.
- Next task: Draft the product page.
```

The deadline is 14 days after the last local edit unless the project sets `review_after` or `review_due`. No recognized GitHub remote or incomplete retrieval means empty output. A project note whose exact read exceeds 65,536 characters arrives as [JSON chunks](https://fmind.github.io/brain-framework/docs/retrieval/#large-exact-reads); the hook assembles them and prints nothing if their digest changes. Test the preview before adding a host hook.

## Connect the host

[Claude Code](https://code.claude.com/docs/en/hooks#sessionstart) adds a `SessionStart` hook's standard output to the session context. In `~/.claude/settings.json`:

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

For an existing action, the [bf-action handoff guide](../../skills/bf-action/references/handoff.md) adds a size checker and an optional Claude Code check after compaction or resume. It reports Context/Resume refs and counts without inserting their text or saving a transcript; refresh the action through the agent before planned compaction.

## Contract

`tests/test_adapters_hooks.py` checks every example with fake `git` and `bf` executables.

- Python 3.11+ standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Read-only and offline: `git remote get-url origin` and `bf read` with literal argv, bounded time and output.
- Never block a session: any failure, incomplete page (`problems`, `stale`, including the project metadata page) or unknown repository prints nothing and exits 0. The repository read, its chunks and the project pages share one 20-second lookup budget, with at most 100 chunks and 100 pages; an incomplete or invalid continuation also prints nothing. Match project metadata by both brain and ref so identical filenames in different brains never share context.
- A few hundred bytes of context: authored-note titles and refs only, refs in code spans no backtick can close. Collected records are counted, never quoted: when a record owns the repository, the hook says so without printing its ref. The agent reads refs explicitly when it needs them.
