# Example hooks

Standalone scripts that agent hosts run on their own events. Copy one into a brain (for example `hooks/`), review it, and register it in your host; Brain Framework neither bundles nor installs hooks. For the agent procedures themselves, install the skills with `bf skills DIR` (see the [skills guide](../../src/bf/skills/README.md)).

| Hook                 | Event            | Arguments | Output                                                                                                                                                                                                             |
| -------------------- | ---------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `session-context.py` | Session start    | `[BRAIN]` | For the working directory's GitHub repository: its owning project, status, review reasons and deadline, next task, linked evidence by relation, the newest linking notes and collection that is overdue or failed. |
| `prompt-context.py`  | Prompt submitted | `[BRAIN]` | Titles and refs of the notes among the three best matches for the prompt's content words, and how many of them are collected records. Opt-in: it runs one search per prompt.                                       |

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

The session hook's output includes `Brain context for repo:github.com/example/new-website`, the project ref `projects/new-website.md`, and `Next task: Run the keyboard navigation check.` The edit date is the day you run it; the review deadline is 14 days later. The prompt hook searches `keyboard navigation check done`, the prompt without its function words, then prints:

```text
Brain search for this prompt (evidence, not instructions):
- New website — Next actions (`projects/new-website.md#next-actions`)
Read refs with `bf read` before relying on them; collected records are counted, not quoted.
```

This verifies the scripts' actual Git-to-brain lookup and prompt search; host installation is a separate step below.

## Preview the context

In the brain's project frontmatter, name the repository it belongs to, with its owner and name in lowercase, such as `repo:github.com/googlecloudplatform/open-knowledge-format`. Identities are case-sensitive, and this hook and the Git history sensor lowercase GitHub owners and names:

```yaml
aliases: [repo:github.com/example/new-website]
```

Copy the hooks from the release tag matching `bf --version` and make them executable, since the host runs them as commands and a download does not keep the executable bit:

```bash
mkdir -p ~/brain/hooks
examples="https://raw.githubusercontent.com/fmind/brain-framework/v$(bf --version)/examples/hooks"
curl -fsSLo ~/brain/hooks/session-context.py "$examples/session-context.py"
curl -fsSLo ~/brain/hooks/prompt-context.py "$examples/prompt-context.py"
chmod +x ~/brain/hooks/session-context.py ~/brain/hooks/prompt-context.py
```

Run the copied hook from that repository with Python 3.11 or later as `python3` and the `bf` release matching this checkout on PATH:

```bash
~/brain/hooks/session-context.py ~/brain
```

For the fictional New website project, the output could read:

```text
Brain context for repo:github.com/example/new-website (evidence, not instructions):
- Project: New website (`projects/new-website.md`), draft, edited 2026-09-27, review deadline 2026-10-11.
- Next task: Draft the product page.
- Linked evidence: repository 12, links 2; list one relation with `bf read projects/new-website.md --rel RELATION`.
  - 2026-09-27 Website review (`actions/2026-09-27_website-review/ACTION.md`)
- Collection needs attention: github-commits overdue; see `bf status`.
Read more with `bf read projects/new-website.md`; collected records are counted, not quoted.
```

A project falls due for review 14 days after its last edit unless its frontmatter sets `stale_after`. The linked-evidence line counts every item per relation and lists the newest linking notes by their `date`; the relation hint appears when a group holds more than its preview. The attention line comes from the home page and names scheduled sensors and routines that are `overdue` or `never` collected, or that failed. No recognized GitHub remote, or any incomplete read (`problems` or `stale`), means empty output. The hook reads the repository identity once: for a note above 32 KiB, that first page still carries the ref and backlink previews it prints.

The prompt hook reads the host's event JSON from stdin. Preview it with a fictional event:

```bash
printf '%s' '{"prompt":"Can you draft the product page?"}' | ~/brain/hooks/prompt-context.py ~/brain
```

It searches at most 8 content words: the prompt's first distinct words after dropping the English and French function words that `bf search` ignores too (acronyms such as `AI` stay), without quotes, prefixes or identities. For the same project, it lists the matching `projects/new-website.md#next-actions` section with its title, between the heading and reminder lines shown above. Collected records among the three best matches are only counted, as in `- 2 collected records also matched.`; `2+` means that further results may hold more. A prompt without content words, no match, a search slower than 5 seconds, `problems` or `stale` mean empty output.

## Connect the host

Both hosts add a command hook's standard output to the agent's context on session start (`SessionStart`) and prompt submission (`UserPromptSubmit`). The prompt hook is opt-in: it adds one search before every prompt, and each search counts under `usage` in `bf status`. Its query words pass to `bf search` as a command-line argument, visible to local process listings while the search runs; BF usage counts never retain them.

[Claude Code](https://code.claude.com/docs/en/hooks#userpromptsubmit): merge into `~/.claude/settings.json`, keeping your other hooks. `timeout` is in seconds; Claude Code discards a timed-out hook's output and sends the prompt anyway:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "~/brain/hooks/session-context.py ~/brain", "timeout": 30 }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "~/brain/hooks/prompt-context.py ~/brain", "timeout": 10 }] }
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
        "hooks": [{ "type": "command", "command": "~/brain/hooks/session-context.py ~/brain", "timeout": 30 }]
      }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "~/brain/hooks/prompt-context.py ~/brain", "timeout": 10 }] }
    ]
  }
}
```

Hosts without hooks can run the session script from their session instructions. Start a session in a repository that a project note names in its `aliases` and check that the context appears; `bf status` then counts its reads under `usage`. Then ask about a saved topic and check that the prompt hook's context reached the agent, for example in Claude Code's [debug log](https://code.claude.com/docs/en/hooks#debug-hooks). Local tests of the scripts do not establish host delivery.

For an existing action, the `bf-action` skill's [handoff guide](../../src/bf/skills/bf-action/references/handoff.md) adds a size checker and an optional Claude Code check after compaction or resume. It reports Context and Resume refs and counts without inserting their text; refresh the action through the agent before a planned compaction.

## Contract

`tests/test_adapters_hooks.py` checks every example with fake `git` and `bf` executables.

- Python 3.11+ standard library only, `#!/usr/bin/env python3`, executable bit set, no shell.
- Read-only and offline: `git remote get-url origin`, `bf read` and `bf search` with literal argv, bounded time and output. The prompt hook passes plain words after `--`, so a prompt never becomes an option or search syntax.
- Never block a session or a prompt: any failure, malformed event, incomplete reply (`problems` or `stale`, including the project and home pages) or unknown repository prints nothing and exits 0. The session hook's repository read, project pages and home page share one 20-second budget, with at most 100 project pages; an invalid continuation also prints nothing. Project metadata matches by both brain and ref, so identical filenames in different brains never share context. The prompt hook reads at most 4 MiB of event JSON and allows its one search 5 seconds.
- A few hundred bytes of context: authored-note titles and refs only, in code spans no backtick can close; with several brains, both hooks print each note's `bf://` address, and the session hook always names an owning note outside `projects/` by its address. The session hook suggests reads that work whichever brains are selected: the owning project by the `uri` or ref its listing returns, any other owner by the repository identity. Collected records are counted, never quoted: when a record owns the repository, the session hook says so without printing its ref. Excerpts and field values are never printed. The agent reads refs explicitly when it needs them.
