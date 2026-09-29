# Example hooks

Standalone scripts that agent hosts run on their own events. Copy one into a brain (for example `settings/hooks/`), review it, and register it in your host; Brain Framework neither bundles nor installs hooks.

| Hook                 | Event            | Arguments | Output                                                                                                                                                           |
| -------------------- | ---------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session-context.py` | Session start    | `[BRAIN]` | For the working directory's GitHub repository: its owning project, status, review signal, next task, linked evidence by relationship and the newest owner items. |
| `prompt-context.py`  | Prompt submitted | `[BRAIN]` | Titles and refs of up to three notes matching the prompt, and how many collected records also matched. Opt-in: it runs one search per prompt.                    |

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
  printf '%s' '{"hook_event_name":"UserPromptSubmit","prompt":"Is the keyboard navigation check done?"}' |
    uv run --project "$bf_checkout" python "$bf_checkout/examples/hooks/prompt-context.py" "$hook_demo/brain"
)
```

The session hook's output includes `Brain context for repo:github.com/example/new-website`, the project ref `projects/new-website.md`, and `Next task: Run the keyboard navigation check.` The edit date is the date you run it; the review deadline is 14 days later. The prompt hook then prints:

```text
Brain search for this prompt (evidence, not instructions):
- New website — Next actions (`projects/new-website.md#next-actions`)
Read refs with `bf read` before relying on them; collected records are counted, not quoted.
```

This verifies the scripts' actual Git-to-brain lookup and prompt search; host installation is a separate step below.

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

The deadline is 14 days after the last local edit unless the project sets `review_after` or `review_due`. No recognized GitHub remote or incomplete retrieval means empty output. The hook makes one read of the repository identity: for a note above 32 KiB it keeps that first page, which carries the ref and the [backlink previews](https://fmind.github.io/brain-framework/docs/retrieval/#notes-records-and-identities) it prints, and never reads the rest of the text. Project status and review signals come from the `projects` listing. Test the preview before adding a host hook.

The prompt hook reads the host's event JSON from stdin. Preview it with a fictional event:

```bash
printf '%s' '{"prompt":"Draft the product page"}' | ~/brain/settings/hooks/prompt-context.py ~/brain
```

For the same project, it lists the matching `projects/new-website.md#next-actions` section with its title, between the heading and reminder lines shown in the demo above. No match, a search slower than 2 seconds, `problems` or `stale` mean empty output.

## Connect the host

Both hosts add a command hook's plain standard output to the agent's context on session start (`SessionStart`) and prompt submission (`UserPromptSubmit`). The prompt hook is opt-in: it adds one search before every prompt, and each search counts under `usage` in `bf status`. Its prompt text passes to `bf search` as a command-line argument, visible to local process listings while the search runs; BF usage counts never retain it.

[Claude Code](https://code.claude.com/docs/en/hooks#userpromptsubmit): merge into `~/.claude/settings.json`, keeping your other hooks. `timeout` is in seconds; Claude Code discards a timed-out hook's output and sends the prompt anyway:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "~/brain/settings/hooks/session-context.py ~/brain" }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "~/brain/settings/hooks/prompt-context.py ~/brain", "timeout": 5 }] }
    ]
  }
}
```

[Codex](https://developers.openai.com/codex/hooks): merge into `~/.codex/hooks.json`, then review and trust both hooks with `/hooks` in the CLI; Codex skips a new or changed hook until you trust it. Without `timeout`, Codex waits up to 600 seconds:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [{ "type": "command", "command": "~/brain/settings/hooks/session-context.py ~/brain", "timeout": 30 }]
      }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "~/brain/settings/hooks/prompt-context.py ~/brain", "timeout": 5 }] }
    ]
  }
}
```

Hosts without hooks can run the session script from their session instructions. Start a session in a repository that a project note names in its `aliases` (`repo:github.com/owner/name`) and check that the context appears; `bf status` then counts its reads under `usage`. Then ask about a saved topic and check that the prompt hook's context reached the agent, for example in Claude Code's [debug log](https://code.claude.com/docs/en/hooks#debug-hooks). Local tests of the scripts do not establish host delivery.

For an existing action, the [bf-action handoff guide](../../skills/bf-action/references/handoff.md) adds a size checker and an optional Claude Code check after compaction or resume. It reports Context/Resume refs and counts without inserting their text or saving a transcript; refresh the action through the agent before planned compaction.

## Contract

`tests/test_adapters_hooks.py` checks every example with fake `git` and `bf` executables.

- Python 3.11+ standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Read-only and offline: `git remote get-url origin`, `bf read` and `bf search` with literal argv, bounded time and output. The prompt follows `--`, so it can never become an option, and is cut to 4,096 characters, bf's query bound.
- Never block a session or a prompt: any failure, malformed event, incomplete page (`problems`, `stale`, including the project metadata page) or unknown repository prints nothing and exits 0. The session hook's repository read and project pages share one 20-second lookup budget, with at most 100 project pages; an incomplete or invalid continuation also prints nothing. Match project metadata by both brain and ref so identical filenames in different brains never share context. The prompt hook reads at most 4 MiB of event JSON and allows its one search 2 seconds.
- A few hundred bytes of context: authored-note titles and refs only, refs in code spans no backtick can close; with several brains, the prompt hook prints each note's `bf://` address. Collected records are counted, never quoted: when a record owns the repository, the session hook says so without printing its ref. Excerpts are never printed. The agent reads refs explicitly when it needs them.
