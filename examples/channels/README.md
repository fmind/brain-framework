# A complete assistant messaging loop

This fictional brain connects a passive local channel to an assistant. `printf` stands in for the agent CLI and answers `Acknowledged.`; the channel records outgoing messages under `logs/deliveries.jsonl`. No model, account or network is needed. Replace the commands with your own provider adapter and harness after testing the contracts.

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
    env -u BF_BRAIN TZ=UTC XDG_CONFIG_HOME="$channel_demo/config" XDG_STATE_HOME="$channel_demo/state" \
      uv run --project "$bf_checkout" bf "$@"
  }
  bf validate
  bf channel read local --since 2026-10-16T08:00:00Z --until 2026-10-16T10:00:00Z
  bf queue
  printf '%s\n' 'Please check.' | bf run personal --stdin
  bf update
  cat logs/deliveries.jsonl
  bf read memories/personal
  printf '%s\n' '{"id":"demo-send","text":"The review is ready."}' | bf channel send local --stdin
  printf '%s\n' '{"id":"demo-retry","text":"Sent after the outage."}' |
    CHANNEL_DEMO_OFFLINE=1 bf channel send local --stdin || echo "The send failed; its delivery waits in the queue."
  bf queue demo-retry
  bf queue demo-retry --retry
  wc -l <logs/deliveries.jsonl
)
```

Expected observations:

1. Validation succeeds. The history read returns `hello-1` in `thread-1`; the queue still has no jobs.
1. The manual run returns `Acknowledged.` and sends one message to the configured endpoint's default conversation.
1. The first update runs the named review schedule and sends another message. The two lines in `logs/deliveries.jsonl` have distinct stable delivery IDs and an empty `reply_to`.
1. `bf read memories/personal` lists both saved answers, with `"total":2`.
1. `bf channel send` uses the endpoint directly, without an assistant: it saves the delivery, sends it and prints `"state":"delivered"` with the fixture's receipt, `"id":"sent-demo-send"`.
1. With `CHANNEL_DEMO_OFFLINE` set, the fixture refuses to send, standing in for a provider outage: `bf channel send` exits 1, its reply shows `"state":"uncertain"`, since the fixture's `send` does not declare `idempotent: true`, with the `error`, and the fixture's message goes to `logs/channel.local.send.log`. `bf queue demo-retry` opens the saved delivery, text included, and `--retry` sends that same delivery once the outage is over: `{"id":"demo-retry","status":"delivered"}`. Watch and `bf update` never retry such a standalone send on their own.
1. `logs/deliveries.jsonl` ends with four lines: the failed attempt recorded nothing.

To try incoming messages, copy this brain to a directory you keep, select it explicitly and run:

```bash
bf watch --brain /path/to/channel-demo --json
```

The fixture emits `hello-1` immediately. Watch durably accepts it, acknowledges the stream, runs the assistant and sends `Acknowledged.` with `conversation: thread-1` and `reply_to: hello-1`. A scheduled review can run beside it. Stop with Ctrl-C. Restarting watch redelivers the fixture message, but its stable ID creates no second reply. The automated example test checks these observable results and stops the subprocess.

[Channel contracts](../../docs/docs/channels.md) define send receipts, bounded reads and stream ACKs. [Assistant configuration](../../docs/docs/prompts.md) explains schedules, trigger coalescing, reply routing, fan-out and recovery. The fixture is deliberately a local demonstration, not a production provider adapter.
