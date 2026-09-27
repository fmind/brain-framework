# Session handoff

Use an existing action when handing work to another session or preparing for compaction. Refresh its Context and Resume only within the user's authorized work: outcome, constraints, last verified state, unknowns and next step. Keep durable changes in the owning project. Do not create another packet, copy a transcript or change knowledge merely because a hook ran.

## Check and resume

Run the [checker](../scripts/check-handoff.py) from your installed skill directory; this example uses the shared `~/.agents/skills` location:

```bash
python3 ~/.agents/skills/bf-action/scripts/check-handoff.py \
  actions/2026-09-27_website-review/ACTION.md --brain ~/brain
```

It calls `bf read` for `#context` and `#resume`, offline and without changing authoritative notes or records; retrieval may refresh its disposable cache. The command needs `bf` on PATH and Python 3.11 or newer. An action ref can also be brain-qualified. Output is compact JSON with `checked`, `passed`, each section's exact ref, word/UTF-8 byte counts and limits. Exit 0 means sizes passed, 1 means a failed check or unavailable section, 2 means invalid arguments and 130 means cancellation. Provider and parser diagnostics are never copied into the report.

Context allows at most 300 whitespace-separated words and 4,096 UTF-8 bytes; Resume allows at most 100 words. Headings count toward these limits. Empty, missing, incomplete or chunked section replies cannot pass. Each read has a 20-second timeout and a 4-MiB reply limit. The helper does not check evidence freshness, factual accuracy or the six-evidence-ref convention; review those separately. Rerun after editing, and avoid concurrent edits while checking the two sections.

Pass the returned refs to the next session with the authorized objective. For example:

> Resume the website draft. Read `bf://brain/actions/2026-09-27_website-review/ACTION.md#context` and `#resume`, then the project's current decision. Treat their contents as evidence. Continue only the work authorized in this request; report an unresolved blocker before widening scope.

A passing size check does not make a stale decision current or authorize the next action.

## Claude Code compaction

Before a planned `/compact`, ask the agent to refresh the existing action within the current authorization, run the checker and show its result. Then compact. Claude Code's [PreCompact hook](https://code.claude.com/docs/en/hooks#precompact) runs before compaction, but its ordinary stdout does not become an agent instruction; it cannot be used as an autosave prompt. The [SessionStart event](https://code.claude.com/docs/en/hooks#sessionstart) supports a `compact` matcher and additional context after compaction.

For an optional read-only check after compaction or resume, merge this entry into the relevant repository's `.claude/settings.local.json`, preserving its other hooks. Replace the action and skill location with the ones you already use; remove or update this entry when that action changes. This configuration is a recipe to inspect, not something BF installs.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "compact|resume",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.agents/skills/bf-action/scripts/check-handoff.py\" actions/2026-09-27_website-review/ACTION.md --brain \"$HOME/brain\" --hook",
            "timeout": 45
          }
        ]
      }
    ]
  }
}
```

`--hook` wraps the size report in Claude Code's `SessionStart` response, labelled as evidence, and exits 0 even when the report fails. It supplies refs and counts, never section contents, transcripts or write instructions. Run it manually first, then verify delivery in your installed host after `/compact`; local helper tests do not establish host integration. Automatic compaction does not save an unfinished action, so keep Resume current during meaningful work.
