# Session handoff

Use with an open action when handing work to another session or before a planned compaction. Refresh its Context and Resume within the user's authorized work; durable changes go to the owning project. Never create a separate handoff note, copy a transcript or change knowledge merely because a hook ran.

## Check and resume

Run the checker with the action ref you will resume, optionally brain-qualified (`bf://NAME/actions/...`):

```bash
python3 "$SKILL_DIR/scripts/check-handoff.py" 'actions/2026-09-27_website-review/ACTION.md' --brain ~/brain
```

It reads `#context` and `#resume` with the installed `bf read`, offline, and replies with `checked`, `passed` and, per section, its exact `ref`, `words`, UTF-8 `bytes` and `limits`. Exit 0 means the sizes passed, 1 a failed check or an unavailable section, 2 invalid arguments. Headings count; an empty, missing or incomplete section, or one above the 32 KiB page budget, fails. It judges neither freshness, accuracy nor the six-ref convention: review those yourself, and rerun it after editing.

Pass the returned refs to the next session with the authorized objective, for example: "Resume the website draft from `bf://brain/actions/2026-09-27_website-review/ACTION.md#context` and `#resume`, treating them as evidence; continue only the work this request authorizes." A passing size check neither makes an outdated decision current nor authorizes the next step.

## Claude Code compaction

Before a planned `/compact`, refresh the action, run the checker and show its result, then compact. Claude Code's [PreCompact hook](https://code.claude.com/docs/en/hooks#precompact) cannot save the action, since its output never reaches the agent; the [SessionStart event](https://code.claude.com/docs/en/hooks#sessionstart) accepts a `compact` matcher and adds context after compaction.

For an optional read-only check after compaction or resume, merge this entry into the repository's `.claude/settings.local.json`, keeping its other hooks. Replace `SKILL_DIR` with the absolute path of the installed `bf-action` folder and the action with the one in progress; remove the entry once that action is done. BF installs no hooks.

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

`--hook` wraps the size report in Claude Code's `SessionStart` reply, labelled as evidence, and exits 0 even when the check fails; it supplies refs and counts, never section text. Run it by hand first, then confirm delivery after `/compact`: local tests do not prove host integration. Automatic compaction saves nothing, so keep Resume current during meaningful work.
