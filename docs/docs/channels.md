---
title: Channels
description: Connect any messaging surface through passive send, read and stream commands owned by the brain.
---

# Channels

A channel is a named endpoint with optional `send`, `read` and `stream` capabilities. Its script owns provider access, sender admission, destination restrictions and transport details. BF understands JSON envelopes and argv, not Google Chat, Slack, email or desktop notifications. Put scripts under `channels/`, or use an executable already on PATH. One script can serve several channel names with different arguments.

Channels have no schedule or polling interval. An [assistant](prompts.md) chooses its inputs, outputs and batch-read cadence. A stream adapter implements push, Pub/Sub, long polling or fast polling itself. Its stdin control frames and stdout messages are bidirectional transport control; application replies go through `send`.

```yaml
# https://fmind.github.io/brain-framework/docs/channels/
channels:
  chat-home:
    send:
      command: [python3, channels/chat.py, send, home]
      idempotent: true
    read:
      command: [python3, channels/chat.py, read, home, "{{start}}", "{{end}}"]
    stream:
      command: [python3, channels/chat.py, stream, home]
  desktop:
    send:
      command: [python3, channels/desktop.py, send]
```

At least one capability is required. Each has `command`, `timeout` and `max_bytes`. Finite commands default to 300 seconds and 64 MiB. A stream defaults to a 65,536-byte frame limit and 30 seconds for a blocked acknowledgement pipe; an idle healthy stream has no total runtime deadline. Commands run from the brain directory without a shell. `BF_BRAIN` identifies that brain; startup-injection variables are stripped. Stderr goes to bounded `logs/channel.NAME.OPERATION.log`, never the protocol output.

## Message envelope

A read or stream returns admitted messages in this shape:

```json
{
  "id": "message-42",
  "sender": "owner-id",
  "conversation": "thread-7",
  "time": "2026-10-16T09:00:00Z",
  "text": "What needs my attention?"
}
```

`id`, `sender`, `conversation`, `time` and `text` are required. IDs must be stable across redelivery. IDs and routing fields are bounded to 4,096 characters and cannot contain control/format characters; text is limited to 32,768 characters. Optional fields are `reply_to`, `cursor` and `own` (default false). `own: true` suppresses the bot's output. Filter bot echoes in the adapter even though BF also checks known send receipts: an echo can arrive before its send returns.

The adapter must verify provider authenticity and authorize the sender before emitting. Preserve sender identity; do not label every incoming message as the owner. Conversation identity is scoped by the channel, so another channel's identical thread ID does not share context. A provider edit that reuses an ID with changed content is rejected: represent admitted edits with a new stable revision ID or keep the original message unchanged.

## Send

`send` receives one JSON object on stdin, then exits with one JSON receipt on stdout:

```json
{ "id": "stable-delivery-id", "text": "Here is the answer.", "conversation": "thread-7", "reply_to": "message-42" }
```

```json
{ "id": "provider-message-43", "conversation": "thread-7" }
```

An omitted/empty conversation requests the configured endpoint's default destination, such as a new thread in one fixed chat. A reply carries its original conversation and message ID; the receipt must preserve the requested conversation. A desktop adapter can return its own stable notification ID and a fixed conversation identifier. Notification is a send-only channel, not a special BF transport.

Declare `idempotent: true` only when repeating an identical delivery ID and payload cannot create another visible message. Use the provider's idempotency mechanism or a durable adapter implementation. BF persists the ID before calling send, reuses it on retry and rejects reuse for different manual content. The default is false: failed or interrupted sends are uncertain and require inspection. Saved answers are never regenerated to retry delivery. A deliberately new send needs a new ID.

Use a configured endpoint directly from the brain directory:

```bash
printf '%s\n' '{"text":"The review is ready."}' | bf channel send desktop --stdin
```

The command creates a delivery ID if omitted, saves the obligation, attempts that delivery and prints its state/receipt. A non-delivered result exits 1. A provider length limit should fail visibly and leave the saved answer intact; silently truncating answers can remove essential qualifications.

## Read

`read` is a finite, complete `[start,end)` batch. BF expands `{{start}}` and `{{end}}` in argv; the script must finish all provider pagination and print one object:

```json
{
  "messages": [
    {
      "id": "message-42",
      "sender": "owner-id",
      "conversation": "thread-7",
      "time": "2026-10-16T09:00:00Z",
      "text": "What needs my attention?"
    }
  ]
}
```

A batch holds at most 1,000 unique messages, all inside its requested window. Incomplete results, duplicate IDs, extra pagination fields and out-of-window messages fail without advancing input progress. Fail rather than returning a partial successful batch. Split an oversized manual history request into smaller windows.

```bash
bf channel read chat-home --since 2026-10-16T08:00:00Z --until 2026-10-16T10:00:00Z
```

Manual reads invoke no assistant and move no subscription checkpoint. An assistant `mode: read` input requires `every` seconds. Its first activation starts at the current time; later calls overlap the last 30 seconds to catch modest provider indexing delays and deduplicate by message ID. This is a bounded lateness allowance, not a guarantee for arbitrary delayed history. Use a durable stream for reliable provider event delivery or fetch older history explicitly.

## Stream

A stream is a long-lived subprocess. BF writes a resume frame on stdin:

```json
{ "type": "resume", "since": "2026-10-16T09:00:00Z", "cursor": "" }
```

The adapter emits one message envelope per stdout line and flushes. After committing the message, every configured recipient and its transport checkpoint to the local queue, BF sends:

```json
{ "type": "ack", "id": "message-42", "cursor": "" }
```

Only then should a Pub/Sub adapter acknowledge the provider message. ACK means durable acceptance, not a completed model answer. On process failure, unacknowledged input must be redelivered with the same ID. A cursor is opaque to BF; preserve the provider's resume/ack semantics and advance it only through accepted frames. A push adapter can drain its retained backlog; BF does not discard older pushed messages by timestamp. A polling adapter can use `since` for its initial position.

Adapters must read control frames continuously or emit one message and wait for its ACK. BF bounds frame and ACK buffers, restarts failed transports with 1–60-second backoff, and kills their process groups when watch exits. Provider retry, subscription renewal and acknowledgement-deadline extension belong to the adapter. Do not run two consumers against the same exclusive provider subscription; BF owns one stream per used channel in one brain, not across separate brains.

## Queue and recovery

`bf queue` lists job/delivery identities, states and errors without message bodies. `bf queue ID` explicitly opens a job or delivery, including private input/output. `bf status --check` diagnoses unresolved work and transport errors. Internal events require no declared registry.

```bash
bf queue
bf queue JOB_ID
bf queue JOB_ID --retry
bf queue DELIVERY_ID --retry
bf queue JOB_ID --cancel
```

A job retry resumes its saved answer/preparation when available; otherwise it permits another harness attempt, which can repeat external tool effects. Inspect first. A delivery retry immediately attempts only that saved delivery, so standalone sends need no assistant or watcher. Cancellation retains identity tombstones and unblocks subsequent work; cancelling a scheduled job skips its window. Existing failed work remains visible if its assistant is removed.

The SQLite queue lives in the private BF state directory under `$XDG_STATE_HOME/bf/runtime/` (normally `~/.local/state/bf/runtime/`), keyed by physical brain identity and bound to its path/name. It is **durable state**, unlike the disposable `.bf/` search cache or run counters. Preserve and back it up with the brain; do not delete it to recover from corruption. Drain pending work before renaming/moving a brain or changing execution configuration. There is one current queue format, with no automatic migration.

The queue admits at most 10,000 unfinished obligations, including reserved fan-out slots, and caps its database at 256 MiB. Conservative byte reservations for each configured `max_bytes`, future deliveries and existing database pages can stop admission earlier. Reduce batch sizes or answer bounds when a batch cannot fit; resolve existing work before admitting more. These checks cannot prevent external disk or filesystem failures. Completed job, event, delivery and conversation bodies each have an 8 MiB retention budget; jobs with unresolved deliveries retain their bodies against the backlog reservation instead; small identity/receipt tombstones remain to prevent replay, and the conversation cache also holds at most 10,000 messages. Persistent backlog or exhausted space stops acceptance visibly, leaving unacknowledged provider input for recovery. Body compaction runs on admission and completion as well as during watch; exact historic job bodies can expire while their evidence records remain.

Queued jobs snapshot their prompt, harness and output configuration. Restoring the reviewed configuration lets an interrupted job resume; cancellation deliberately abandons it. Editing a channel cannot silently send old work to a new destination. The guarantee is durable local acceptance and conservative recovery, not universal exactly-once execution: only a provider/adapter idempotency guarantee makes send retries safe.
