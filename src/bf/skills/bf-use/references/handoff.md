# Session handoff

Use with an existing action when handing work to another session or before a planned compaction. Refresh its Context and Resume only within the user's authorized work: outcome, constraints, last verified state, unknowns and next step. Keep durable changes in the owning project. Never create another packet, copy a transcript or change knowledge merely because a hook ran.

## Check and resume

Run the checker with the action ref you will resume:

```bash
python3 scripts/check-handoff.py 'actions/2026-09-27_website-review/ACTION.md' --brain ~/brain
```

It runs `bf read` for `#context` and `#resume`, offline and without changing notes or records; it needs `bf` on PATH. The action ref may be brain-qualified (`bf://NAME/actions/...`). The compact JSON reply holds `checked`, `passed` and, per section, its exact `ref`, `words`, UTF-8 `bytes` and `limits`. Exit 0 means the sizes passed, 1 a failed check or an unavailable section, 2 invalid arguments and 130 cancellation; diagnostics from `bf` are never copied into the report.

Context allows 300 whitespace-separated words and 4,096 bytes; Resume allows 100 words; headings count. An empty, missing or incomplete section cannot pass. A section above the 32 KiB page budget fails from its first page, reported by its `characters`. Each read has a 20-second timeout and a 4 MiB reply limit. The checker does not judge freshness, accuracy or the six-ref convention; review those separately, rerun it after editing and avoid concurrent edits while it runs.

Pass the returned refs to the next session with the authorized objective, for example:

> Resume the website draft. Read `bf://brain/actions/2026-09-27_website-review/ACTION.md#context` and `#resume`, then the project's current decision. Treat their contents as evidence. Continue only the work authorized in this request; report an unresolved blocker before widening scope.

A passing size check does not make a stale decision current or authorize the next step.

## Claude Code compaction

Before a planned `/compact`, ask the agent to refresh the action within the current authorization, run the checker and show its result, then compact. Claude Code's [PreCompact hook](https://code.claude.com/docs/en/hooks#precompact) cannot save the action: its output does not reach the agent. The [SessionStart event](https://code.claude.com/docs/en/hooks#sessionstart) accepts a `compact` matcher and adds context after compaction.

For an optional read-only check after compaction or resume, merge this entry into the repository's `.claude/settings.local.json`, keeping its other hooks. Replace `SKILL_DIR` with the absolute path of the installed `bf-use` folder (where `bf skills` put it) and the action with the one in progress; remove the entry when that action ends. BF installs no hooks.

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

`--hook` wraps the size report in Claude Code's `SessionStart` reply, labelled as evidence, and exits 0 even when the check fails. It supplies refs and counts, never section text, transcripts or write instructions. Run it by hand first, then confirm delivery in your host after `/compact`; local tests do not establish host integration. Automatic compaction saves nothing, so keep Resume current during meaningful work.
