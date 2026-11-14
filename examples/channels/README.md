# A complete messaging loop

This fictional brain connects a local message job to prompt jobs. `printf` and `cat` stand in for the agent CLI, while `channels/local.py` acts as a local provider adapter writing outbound messages to `logs/deliveries.jsonl`. The walkthrough ends with a read-only `scout` proposing steps and an `actor` receiving the owner's reply in `owner-chat`. No model, account, or network is needed.

From the framework checkout, run this isolated walkthrough:

```bash
(
  set -eu
  bf_checkout=$PWD
  channel_demo=$(mktemp -d)
  channel_demo=$(cd "$channel_demo" && pwd -P)
  trap 'rm -rf -- "$channel_demo"' EXIT
  cp -R examples/channels "$channel_demo/brain"
  cd "$channel_demo/brain"
  bf() {
    env -u BF_BRAIN TZ=UTC XDG_CONFIG_HOME="$channel_demo/config" \
      uv run --project "$bf_checkout" bf "$@"
  }
  bf validate
  bf run local-poll --since 2026-10-16T08:00:00Z --until 2026-10-16T10:00:00Z
  bf queue
  bf update --job personal
  printf '%s\n' 'Please check.' | bf run personal --stdin
  cat logs/deliveries.jsonl
  printf '%s\n' 'The review is ready.' | bf post --to local --stdin
  mkdir -p logs && : >logs/offline
  printf '%s\n' 'Sent after the outage.' |
    bf post --to local --key demo-retry --stdin || echo "The send failed; its delivery waits in the queue."
  rm -f logs/offline
  bf queue demo-retry
  bf queue demo-retry --retry
  wc -l <logs/deliveries.jsonl
  cat >>bf.yaml <<'YAML'
  scout:
    prompt: prompts/scout.md
    harness: [printf, "The review is ready. Proposed: 1. Rerun the checks. 2. Publish the notes."]
    emit: message
    idempotent: true
    post: owner-chat
    on: { every: 86400 }
  actor:
    prompt: prompts/actor.md
    harness: [cat]
    emit: message
    post: owner-chat
    on:
      event:
        kind: message.received
        source: owner-chat
YAML
  bf update --job scout
  bf update --job owner-chat
  bf update --job actor
  tail -n 2 logs/deliveries.jsonl
)
```

Expected observations:

1. `bf validate` succeeds. `bf run local-poll` admits `"hello-1"` (`"messages":1`) into the durable queue.
1. `bf update --job personal` runs the due scheduled run and delivers `"Acknowledged."` via `local` (`logs/deliveries.jsonl`), and `bf run personal --stdin` sends a second delivery.
1. `bf post --to local --stdin` delivers directly through `local` (`"state":"delivered"`, receipt `"id":"sent-..."`).
1. While `logs/offline` exists, `local.py send` fails before writing to `logs/deliveries.jsonl`: `bf post` exits `1` and leaves `"state":"uncertain"` in the queue (`bf queue demo-retry`). Once `logs/offline` is removed, `bf queue demo-retry --retry` sends `"Sent after the outage."` and returns `{"id":"demo-retry","status":"delivered"}`.
1. `logs/deliveries.jsonl` has four lines (the failed attempt wrote nothing).
1. `scout` posts its proposal (`Proposed: 1. ...`) to `owner-chat`. Next, `bf update --job owner-chat` polls `owner-chat`, admitting the fictional owner reply `"do 2"` in `"proactive-thread"`; then `bf update --job actor` dispatches that `message.received` event to `actor`, whose `cat` harness posts `Invocation:` carrying `events` with `"do 2"` in `"proactive-thread"` back through `owner-chat`.

To try daemon streaming (`on: { daemon: true }`), copy this brain to a directory you keep and run:

```bash
bf watch --brain /path/to/channel-demo --json
```

`channels/local.py stream` emits `hello-1` immediately as NDJSON. Watch durably admits the message (`"message.received"`), invokes `personal`, and records `"Acknowledged."` in `logs/deliveries.jsonl`. Stop with Ctrl-C.

[Assist guide](../../docs/docs/assist.md) defines channel send receipts, bounded polls, NDJSON daemon streams, `{{lines:...}}` allowlists, schedules, `extract:`, wakes, `post:`, and recovery.

`local-poll` also demonstrates a sender allowlist with `{{lines:channels/allowlist.txt}}`: [channels/allowlist.txt](channels/allowlist.txt) lists trusted sender IDs (one per line, `#` comments ignored), which BF expands as trailing arguments to `channels/local.py read` without a shell.
