# Run an assistant offline

A [assistant](../../docs/docs/prompts.md) runs your agent harness over the brain and keeps each reply as a record. This fictional brain replaces the harness with `cat`, which prints the expanded prompt it receives: no model, account or network is needed, and you see exactly what a real harness would be given.

| File                                         | Purpose                                                                                     |
| -------------------------------------------- | ------------------------------------------------------------------------------------------- |
| [bf.yaml](bf.yaml)                           | Declares the `assistant` with `command: [cat]` and no automatic inputs or schedules.        |
| [prompts/assistant.md](prompts/assistant.md) | The prompt file, with the `{{brain}}`, `{{start}}`, `{{end}}` and `{{input}}` placeholders. |
| `projects/new-website.md`                    | A minimal project note, so the brain is not empty.                                          |

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
)
```

Expect, in order:

1. Validation returns `"valid":true`: the prompt file exists and the command is on PATH.
1. The dry run runs nothing: this assistant has no inputs, schedules or triggers. Only `bf run` starts it.
1. The run returns `"status":"ran"`, the `record` it wrote, such as `assistant:20261014T090000Z-1a2b3c4d`, and its reply as `text`. Here the reply is the expanded prompt: the brain's absolute path, a window ending now and `Owner message: What changed in the launch?`.
1. The source page lists that one record, titled `Brain assistant`, the reply's first line without its heading mark.
1. The scoped search finds the record by a word of the owner's message. Like any record, a prompt run is evidence the next run, `bf search` and your agents read back.

## Use your harness

Replace `[cat]` with your harness, such as `[claude, --print, --output-format, json, --allowedTools, "Bash(bf read *),Bash(bf search *)"]` with `text: /result`, then add `schedules: {review: {every: 1800}}` to run it every 30 minutes. [Pass the prompt to the harness](../../docs/docs/prompts.md#pass-the-prompt-to-the-harness) lists the harness settings; [prompt troubleshooting](../../docs/docs/troubleshooting.md#a-prompt-fails) covers failed runs. Swapping a harness back to `[cat]` is a quick way to check what it receives.
