---
icon: lucide/messages-square
description: Connect any messaging surface through passive send, read and stream commands owned by the brain.
---

# Channels and deliveries

Jobs under `jobs:` connect your brain to messaging surfaces — chat, inbox or desktop notifications — using `emit: messages` (polling `run:` or `on: { daemon: true }` streams) and outbound `send:` jobs. BF handles JSON envelopes, durable queueing and retries; your script under `channels/` (or on `PATH`) owns provider credentials, sender authentication and destination bounds.

```yaml
# https://fmind.github.io/brain-framework/docs/channels/
jobs:
  chat-send:
    send: [python3, channels/chat.py, send, home]
    idempotent: true
  chat-read:
    run: [python3, channels/chat.py, read, home, "{{start}}", "{{end}}"]
    emit: messages
    on: { every: 60 }
  chat-stream:
    run: [python3, channels/chat.py, stream, home, "{{cursor}}"]
    emit: messages
    on: { daemon: true }
  desktop:
    send: [python3, channels/desktop.py, send]
```

## Message and send settings

- **`send:` jobs** deliver outgoing text via `bf post` or prompt jobs with `emit: message` + `post:`.
- **`emit: messages` jobs** poll on `on: { every: N }` (with `bf update`/`bf watch`) or stream continuously under `bf watch` with `on: { daemon: true }`.
- Commands run from the brain root without a shell, expand `{{brain}}`, `{{home}}`, `{{state}}`, `{{run}}`, `{{start}}`, `{{end}}`, `{{cursor}}`, `{{text}}`, `{{target}}`, `{{thread}}`, `{{context}}` and `{{lines:FILE}}`, forward `BF_BRAIN` and `requires.env`, and log stderr to `logs/NAME.log`.

## Message envelope

`emit: messages` jobs print either a JSON array, `{"messages": [...], "cursor": "..."}`, or single message JSON objects (one per line for `daemon: true` streams):

```json
{
  "id": "message-42",
  "sender": "owner-id",
  "conversation": "thread-7",
  "time": "2026-10-16T09:00:00Z",
  "text": "What needs my attention?"
}
```

- **Required**: `id` (stable across redelivery), `sender`, `conversation`, `time`, `text` (<= 32,768 chars; routing fields <= 4,096 chars without control/format chars).
- **Optional**: `reply_to`, `cursor`, `own` (`true` drops own messages without emitting `channel.received`).
- Filter bot echoes in the adapter (BF also drops IDs matching the channel's last 35 days of send receipts and duplicate `input:<channel>:<id>` events).

## Send

`send:` reads one JSON delivery envelope on stdin (unless `{{text}}` appears in argv, in which case text passes as an argument) and prints one JSON receipt on stdout:

```json
{ "id": "stable-delivery-id", "text": "Here is the answer.", "conversation": "thread-7", "reply_to": "message-42" }
```

```json
{ "id": "provider-message-43", "conversation": "thread-7" }
```

Omitted or empty `conversation` targets the endpoint's default destination; receipts must preserve a requested `conversation`. Copy [`desktop.py`](https://github.com/fmind/brain-framework/blob/main/examples/channels/channels/desktop.py) for `notify-send` / `osascript` notifications (note: desktop notifier argv is visible in local process listings). Post directly:

```bash
bf post --to desktop "The review is ready."
```

### Retries and idempotency

Set `idempotent: true` on a `send:` job only when repeating the same delivery `id` and payload cannot post duplicate messages. BF persists the delivery ID for 35 days before calling `send:`:

- **Idempotent delivery (`idempotent: true`)**: failed or interrupted deliveries retry automatically in `bf watch` and `bf update` with exponential backoff (2, 4, 8… up to 300s, max 20 attempts) before becoming `failed` (`…; stopped after 20 attempts; retry or cancel with bf queue`).
- **Default delivery (`idempotent: false`)**: failed or interrupted sends become `uncertain` (`interrupted send; inspect before retrying`) and wait until resolved with `bf queue`.
- **Command never started** (such as missing executable) fails without sending and retries when idempotent; a receipt naming a mismatched `conversation` always becomes `uncertain`.

## Read and stream

Polling jobs (`run:` + `emit: messages` + `on: { every }`) run through `bf run JOB` or `bf update`, admitting up to 1,000 messages per batch (`channel.received` events trigger subscribing jobs).

Under `bf watch`, `on: { daemon: true }` jobs run long-lived subprocesses reading stdout line by line (JSON message object, array, or `{messages, cursor}`). Each admitted message records its event and `cursor` checkpoint cleanly in `.bf/runtime.sqlite3`; BF restarts disconnected daemons with backoff and stops their process groups on exit.

## Queue and recovery

`bf queue` lists runs, deliveries and wakes without message bodies; `bf queue ID` opens one item's full payload:

```bash
bf queue
bf queue ENTRY_ID
bf queue ENTRY_ID --retry
bf queue ENTRY_ID --cancel
```

- `--retry` re-queues failed or uncertain deliveries and wakes; `--cancel` marks them cancelled. Uncertain runs (`active_runs`) clear via `--cancel`.
- Crashed processes left `sending` or `running` recover automatically when `bf queue --retry|--cancel` or `bf update`/`bf watch` runs (`interrupted send; inspect before retrying`).

### Where the queue lives

`.bf/runtime.sqlite3` inside `<brain>/.bf/` is **durable state** (`synchronous=EXTRA`): back it up while no BF process runs, keep any `-journal` rollback file (`runtime database holds an interrupted write; run bf queue to recover it`), and never delete it to clear errors. Schema mismatches report `runtime database predates this release; preserve it and follow upgrade steps in the changelog` or `unexpected runtime database schema; preserve it and repair it`.

Capacity and retention bounds:

- Up to 10,000 unfinished deliveries and wakes, 256 MiB database ceiling (`runtime queue has no unresolved-work slots; clear pending work first` or `runtime queue reached its size limit; resolve or clear pending work`).
- Event payloads, completed deliveries and message history trim to newest 8 MiB budgets; receipts and tombstones persist 35 days; unresolved work never expires.

### Move or rename a brain

Drain `bf queue` before moving or renaming a brain:

- Moving or renaming a brain whose `.bf/runtime.sqlite3` still holds unresolved work from its previous path or brain `name` refuses runtime commands (`runtime queue holds unresolved work under a previous path or name; resolve via bf queue first`) until drained, while collection continues unaffected.

### Configuration changes

Queued deliveries snapshot their `send:` argv. Drain `bf queue` before editing `send:`:

- Changing or disabling a `send:` job while deliveries are pending marks retried attempts `uncertain` (`send job configuration changed or is disabled; inspect bf queue before retrying`) so old work never silently routes to a changed command or destination.
