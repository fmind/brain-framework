# Run prompt jobs offline

A [prompt job](../../docs/docs/prompts.md) runs your agent harness over the brain and handles plain text, `NO_REPLY`, JSON `extract:` pointers, and `bf wake --run` / `bf action new --run` calls. This fictional brain replaces the harness with `cat`, `echo`, and `printf`, so no model, account, or network is needed.

| File                                         | Purpose                                                                                                                                             |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| [bf.yaml](bf.yaml)                           | Declares `defaults.post`, a polled `inbox`, a manual `assistant` (`harness: [cat]`), and a `triage` job triggered on schedule and channel events.   |
| [prompts/assistant.md](prompts/assistant.md) | The prompt template with `{{brain}}`, `{{start}}`, `{{end}}`, `{{run}}`, and `{{input}}`, plus `NO_REPLY` and `bf wake` / `bf action new` guidance. |
| `projects/new-website.md`                    | A minimal project note (`updated: 2026-09-27`).                                                                                                     |

Run from the Brain Framework checkout after `uv sync --locked`. This subshell copies the brain, isolates configuration and state, and removes its temporary files on exit:

```bash
(
  set -eu
  bf_checkout=$PWD
  prompt_demo=$(mktemp -d)
  prompt_demo=$(cd "$prompt_demo" && pwd -P)
  trap 'rm -rf -- "$prompt_demo"' EXIT
  cp -R examples/prompts "$prompt_demo/brain"
  cd "$prompt_demo/brain"
  bf() {
    env -u BF_BRAIN TZ=UTC XDG_CONFIG_HOME="$prompt_demo/config" \
      uv run --project "$bf_checkout" bf "$@"
  }
  bf validate
  bf update --dry-run
  echo "What changed in the launch?" | bf run assistant --dry-run --stdin
  echo "What changed in the launch?" | bf run assistant --stdin
  bf run triage
  bf update --job inbox --job triage
  cat >> bf.yaml <<'YAML'
  draft-action:
    prompt: prompts/assistant.md
    harness: [printf, "%s", "---\ntype: action\nstatus: draft\n---\n\n# Launch brief\n\nFollow up on launch date.\n"]
    emit: action
  quiet:
    prompt: prompts/assistant.md
    harness: [printf, NO_REPLY]
    emit: message
  brief:
    prompt: prompts/assistant.md
    harness: [printf, '{"structured_output":{"text":"The launch moved to Friday."}}']
    extract: /structured_output/text
    emit: message
  waker:
    prompt: prompts/assistant.md
    harness: [sh, -c, 'bf wake --job brief --in 600 --reason "Recheck the launch." --run "$1"', sh, "{{run}}"]
    emit: log
  bad-json:
    prompt: prompts/assistant.md
    harness: [printf, '{"status":"RESOURCE_EXHAUSTED"}']
    extract: /structured_output/text
    emit: message
YAML
  bf run draft-action
  bf read actions
  bf search "Launch brief" --scope actions
  bf run quiet
  bf run brief
  bf run waker
  bf run bad-json || echo "Missing JSON pointer failed cleanly without posting."
  bf queue
)
```

Expect, in order:

1. `bf validate` returns `"valid":true`. Validation does not check `cat` or your harness on `PATH` (run `bf doctor` to check binaries, env vars, and scripts).
1. `bf update --dry-run` runs nothing and lists `"manual":["assistant"]` alongside the due `inbox` and `triage` jobs.
1. `bf run assistant --dry-run --stdin` returns the expanded prompt under `"text"`, showing the brain path, run ID, and `Owner message: What changed in the launch?`.
1. `bf run assistant --stdin` records the output in `logs/assistant.log` (`emit: log`).
1. `bf run triage` delivers `"handled"` through `defaults.post` (`inbox`), and `bf update --job inbox --job triage` polls `inbox`, admits its message, and triggers `triage` on the resulting event.
1. `draft-action` writes a validated draft note under `actions/YYYY-MM-DD_draft-action-XXXXXXXX/` (`"action":"actions/..."`), which `bf read actions` lists and `bf search "Launch brief" --scope actions` matches.
1. `quiet` outputs `NO_REPLY`, so BF returns `"quiet":true` and delivers nothing.
1. `brief` demonstrates `extract: /structured_output/text`: BF extracts `"The launch moved to Friday."` from the JSON envelope and posts it to `inbox`.
1. `waker` calls `bf wake --job brief --in 600 --reason "Recheck the launch." --run {{run}}`, leaving `brief` due in 10 minutes inside `bf queue`.
1. `bad-json` returns `{"status":"RESOURCE_EXHAUSTED"}` without `/structured_output/text`; BF exits `1` without posting or saving a broken draft.

## Use your harness

Replace `[cat]` with your CLI harness, such as `[claude, -p, --output-format, json, --allowedTools, "Bash(bf read *),Bash(bf search *)", --permission-mode, dontAsk]` with `extract: /result` and `emit: message` or `emit: action`. Add `on: { every: 30m }` (plus an optional `gate:` script such as [records-gate.py](../routines/records-gate.py)) so the harness only wakes when new evidence arrives. See [Prompts and jobs](../../docs/docs/prompts.md) and [Troubleshooting](../../docs/docs/troubleshooting.md).
