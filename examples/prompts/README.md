# Run an assistant offline

An [assistant](../../docs/docs/prompts.md) runs your agent harness over the brain and keeps each reply as a record. This fictional brain replaces the harness with `cat`, which prints the expanded prompt it receives: no model, account or network is needed, and you see exactly what a real harness would be given.

| File                                         | Purpose                                                                                                                                               |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| [bf.yaml](bf.yaml)                           | Declares the `assistant` with `command: [cat]` and no automatic inputs or schedules.                                                                  |
| [prompts/assistant.md](prompts/assistant.md) | The prompt file, with the `{{brain}}`, `{{start}}`, `{{end}}` and `{{input}}` placeholders, and rules for when to speak, what to do and when to wake. |
| [routines/harness.py](routines/harness.py)   | A wrapper that adds harness arguments, such as wider permissions, only to runs answering a message.                                                   |
| `projects/new-website.md`                    | A minimal project note, so the brain is not empty.                                                                                                    |

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
    env -u BF_BRAIN TZ=UTC XDG_CONFIG_HOME="$prompt_demo/config" XDG_STATE_HOME="$prompt_demo/state" \
      uv run --project "$bf_checkout" bf "$@"
  }
  bf validate
  bf update --dry-run
  echo "What changed in the launch?" | bf run assistant --stdin
  bf read memories/assistant
  bf search "launch" --scope memories/assistant
  cat >> bf.yaml <<'YAML'
  why:
    prompt: prompts/assistant.md
    command: [printenv, BF_TRIGGER]
  brief:
    prompt: prompts/assistant.md
    command: [printf, '{"structured_output":{"say":"The launch moved to Friday.","wake":{"in":60,"reason":"Recheck the launch."}}}']
    reply: {path: /structured_output}
    wake: {min: 600, max: 86400}
  quota:
    prompt: prompts/assistant.md
    command: [printf, '{"status":"RESOURCE_EXHAUSTED"}']
    reply: {path: /structured_output}
  guarded:
    prompt: prompts/assistant.md
    command: [python3, routines/harness.py, --input-arg, --wide, --, echo, narrow]
YAML
  bf run why
  bf run brief
  bf run quota || echo "The harness returned no structured reply; its job waits in the queue."
  bf run guarded
  bf queue
)
```

Expect, in order:

1. Validation returns `"valid":true`: the prompt file exists. Validation does not look up `cat` or your harness on PATH, which varies by machine: a run whose command is missing fails with `COMMAND is not on PATH` and writes no record.
1. The dry run runs nothing and lists the assistant under `manual`, as `"manual":["assistant"]`: it has no inputs or schedules, so only `bf run` starts it.
1. The run returns `"status":"ran"`, the `record` it wrote, such as `assistant:20261014T090000Z-1a2b3c4d`, and its reply as `text`. Here the reply is the expanded prompt: the brain's absolute path, the 24 hours ending now and `Owner message: What changed in the launch?`.
1. The source page lists that one record, titled `Brain assistant`, the reply's first line without its heading mark.
1. The scoped search finds the record by a word of the owner's message. Like any record, an assistant run is evidence the next run, `bf search` and your agents read back.
1. The walkthrough then adds four assistants to `bf.yaml`. `why` runs `printenv BF_TRIGGER`, so its reply is `"text":"manual"`: BF tells the harness [why it runs](../../docs/docs/prompts.md#know-why-it-runs) through environment variables.
1. `brief` stands in for a harness with a [structured reply](../../docs/docs/prompts.md#structured-replies): its JSON holds `say` and `wake` at `/structured_output`. `bf run brief` returns `"text":"The launch moved to Friday."`, and BF clamps its 60-second wake to the 600-second minimum.
1. `quota` stands in for a harness that exits 0 but returns no structured reply, as after a failed model call. `bf run quota` returns `"ok":false` with `"error":"assistant reply at /structured_output is missing or does not match bf schema --kind assistant-reply; see logs/quota.log"`, records nothing and exits 1.
1. `guarded` runs `echo narrow` through [routines/harness.py](routines/harness.py), which adds `--wide` at the end only when `BF_TRIGGER` is `input`. `bf run guarded` is a manual run, so it returns `"text":"narrow"`; a run answering a channel message would print `narrow --wide`. [Propose, then act](../../docs/docs/prompts.md#propose-then-act) explains when to widen a harness's permissions this way.
1. `bf queue` lists that job as `"state":"uncertain"`, because a harness that ran might already have acted: retry or cancel it with `bf queue ID --retry` or `--cancel`. Its `wakes` show `brief` due in about 10 minutes, once `bf watch` or `bf update` runs it.

## Use your harness

Replace `[cat]` with your harness, such as `[claude, --print, --output-format, json, --json-schema, "{{schema}}", --tools, "Read,Grep,Glob,Bash", --allowedTools, "Bash(bf read *),Bash(bf search *)", --permission-mode, dontAsk]` with `reply: {path: /structured_output}`, then add `schedules: {review: {every: 1800}}` to run it every 30 minutes, about 1,440 harness runs a month: pick the interval whose model cost you accept. [Pass the prompt to the harness](../../docs/docs/prompts.md#pass-the-prompt-to-the-harness) lists the harness settings; [assistant troubleshooting](../../docs/docs/troubleshooting.md#a-prompt-fails) covers failed runs. To check later what the harness receives, keep this `[cat]` assistant under another name, without `reply`, and dry-run it with `bf run NAME --dry-run`: swapping the command of an assistant with queued work fails that work until you retry it.
