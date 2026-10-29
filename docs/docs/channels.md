---
icon: lucide/messages-square
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

At least one capability is required. Each has `command`, `timeout` and `max_bytes`. Finite commands default to 300 seconds and 64 MiB. A stream defaults to a 1 MiB frame limit (1 KiB–1 MiB), which holds the largest valid envelope however it is escaped, and 30 seconds for a blocked acknowledgement pipe; an idle healthy stream has no total runtime deadline. Print compact UTF-8 frames, such as Python's `json.dumps(message, ensure_ascii=False)`: a `\uXXXX` escape takes six bytes per character. Commands run from the brain directory without a shell. `BF_BRAIN` identifies that brain; startup-injection variables are stripped. Stderr goes to bounded `logs/channel.NAME.OPERATION.log`, never the protocol output. Each stream run ends there with one `== TIME OUTCOME after N.Ns ==` entry, even when stderr is empty: the outcome is `stopped` or the error, followed by `; adapter exited with status N` or `; adapter was killed by SIGNAL` when the adapter ended on its own.

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

`id`, `sender`, `conversation`, `time` and `text` are required. IDs must be stable across redelivery. IDs and routing fields are bounded to 4,096 characters and cannot contain control/format characters; text is limited to 32,768 characters. Optional fields are `reply_to`, `cursor` and `own` (default false). `own: true` suppresses the bot's output. BF also drops a message whose ID matches one of that channel's send receipts from the last 35 days, but filter bot echoes in the adapter too: an echo can arrive before its send returns. An invalid envelope fails naming the field and the reason, never the value: `channel NAME: invalid stream message envelope: FIELD: REASON`, or `channel NAME: read must return a complete JSON messages batch: messages.N.FIELD: REASON` for a read.

The adapter must verify provider authenticity and authorize the sender before emitting. Preserve sender identity; do not label every incoming message as the owner. Conversation identity is scoped by the channel, so another channel's identical thread ID does not share context. A provider edit that reuses an admitted message's ID keeps the admitted version: the edit starts no job and never stops later messages. To answer edits, give each revision a new stable ID.

## Send

`send` receives one JSON object on stdin, then exits with one JSON receipt on stdout:

```json
{ "id": "stable-delivery-id", "text": "Here is the answer.", "conversation": "thread-7", "reply_to": "message-42" }
```

```json
{ "id": "provider-message-43", "conversation": "thread-7" }
```

An omitted/empty conversation requests the configured endpoint's default destination, such as a new thread in one fixed chat. A reply carries its original conversation and message ID; the receipt must preserve the requested conversation. A desktop adapter can return its own stable notification ID and a fixed conversation identifier. Notification is a send-only channel, not a special BF transport.

Declare `idempotent: true` only when repeating an identical delivery ID and payload cannot create another visible message. Use the provider's idempotency mechanism or a durable adapter implementation. BF persists the ID before calling send and reuses it on retry; for [35 days](#queue-and-recovery), sending that ID again returns the saved delivery when the content is identical and fails when it differs. On an idempotent channel, a failed or interrupted assistant delivery retries on its own after 2, 4, 8… seconds, at most 300, and after 20 attempts becomes `failed` (`…; stopped retrying after 20 attempts; retry or cancel it with bf queue`). Otherwise, the default, a failed or interrupted send is `uncertain` and requires inspection (`interrupted send; inspect before retrying`). A send command that never started, such as one not on PATH, sent nothing, so it fails or retries like an idempotent one; a harness that never started leaves its job `failed`, not `uncertain`. Watch and `bf update` never send or retry a standalone `bf channel send`: when it fails, it is `failed` on an idempotent channel (`…; retry it with bf queue ID --retry`) and `uncertain` otherwise. An explicit `bf queue ID --retry` starts the attempts over. Saved answers are never regenerated to retry delivery. A deliberately new send needs a new ID.

Use a configured endpoint directly from the brain directory:

```bash
printf '%s\n' '{"text":"The review is ready."}' | bf channel send desktop --stdin
```

The command creates a delivery ID if omitted, saves the obligation, attempts that delivery and prints its state/receipt. A non-delivered result exits 1; the [messaging example](https://github.com/fmind/brain-framework/tree/main/examples/channels) shows a failed send and its retry. A provider length limit should fail visibly and leave the saved answer intact; silently truncating answers can remove essential qualifications.

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

A batch holds at most 1,000 unique messages, all inside its requested window. Incomplete results, duplicate IDs, extra pagination fields and out-of-window messages fail without advancing input progress. Fail rather than returning a partial successful batch. A manual `bf channel read` of a window holding more fails: split it into smaller windows.

```bash
bf channel read chat-home --since 2026-10-16T08:00:00Z --until 2026-10-16T10:00:00Z
```

Manual reads invoke no assistant and move no subscription checkpoint. An assistant `mode: read` input requires `every` seconds. Its first activation starts at the current time; later calls overlap the last 30 seconds to catch modest provider indexing delays and deduplicate by message ID. This is a bounded lateness allowance, not a guarantee for arbitrary delayed history. Use a durable stream for reliable provider event delivery or fetch older history explicitly.

A read input whose window holds more than 1,000 messages catches up in parts. It reads again from its checkpoint, without the overlap, halving the window until a part fits, admits that part and moves its checkpoint to the part's end; the next part is twice as long, up to the end of the poll's window. A poll admits at most 16 parts; halving does not count, since it ends at one second. While a backlog remains, watch polls the input again about a second later instead of after `every`, and `bf update` continues at its next run. More than 1,000 messages within one second fail with `channel NAME: more than 1000 messages fall within one second; read them with a stream input`, keeping every part admitted before.

## Stream

A stream is a long-lived subprocess. BF writes a resume frame on stdin:

```json
{ "type": "resume", "since": "2026-10-16T09:00:00Z", "cursor": "" }
```

The adapter emits one message envelope per stdout line and flushes. After committing the message, every configured recipient and its transport checkpoint to the local queue, BF sends:

```json
{ "type": "ack", "id": "message-42", "cursor": "" }
```

Only then should a Pub/Sub adapter acknowledge the provider message. ACK means durable acceptance, not a completed model answer. On process failure, unacknowledged input must be redelivered with the same ID. A cursor is opaque to BF; preserve the provider's resume/ack semantics and advance it only through accepted frames. A push adapter can drain its retained backlog; BF does not discard older pushed messages by timestamp.

`since`, which is also `{{start}}` in a stream command, is the stream's first start and never advances. A polling adapter resumes from `cursor`: it puts its provider position in each envelope's `cursor`, and each later resume frame carries the cursor of the last admitted message. An adapter that leaves `cursor` empty replays everything since that first start on every reconnect, and a replayed message whose 35-day identity tombstone has expired is answered again.

Adapters must read control frames continuously or emit one message and wait for its ACK. BF bounds frame and ACK buffers, restarts failed transports with 1–60-second backoff, and kills their process groups when watch exits. An adapter still running 5 seconds after BF wrote its resume frame counts as connected: its earlier transport error clears from `bf status` and the backoff restarts at 1 second. One that exits sooner keeps its error, and so does a refused admission, such as a full queue, until a message is admitted again. Provider retry, subscription renewal and acknowledgement-deadline extension belong to the adapter. Do not run two consumers against the same exclusive provider subscription; BF owns one stream per used channel in one brain, not across separate brains.

## Queue and recovery

`bf queue` lists job, delivery and sensor trigger identities, states and errors without message bodies; its `triggers` are those still `pending`, with the reason one waits, and those `dropped`. `bf queue ID` explicitly opens a job, delivery or trigger, including private input/output. `bf status --check` fails while an assistant has a failed or uncertain job or schedules or triggers that cannot start, or a channel has an unresolved delivery that failed or is uncertain or a current stream or read error: `attention` names each, and `messaging` holds the details, as [status](commands.md#status-sources-and-routines) describes.

```bash
bf queue
bf queue JOB_ID
bf queue JOB_ID --retry
bf queue DELIVERY_ID --retry
bf queue JOB_ID --cancel
```

A job retry resumes its saved answer/preparation when available; otherwise it permits another harness attempt, which can repeat external tool effects. Inspect first. A delivery retry immediately attempts only that saved delivery, so standalone sends need no assistant or watcher. A trigger retry hands a `pending` or `dropped` trigger to the next watch pass or `bf update`, which drops it again unless its trigger is restored. Cancellation retains identity tombstones and unblocks subsequent work; cancelling a scheduled job skips its window. A `failed` delivery no longer holds later work of its conversation or schedule; an `uncertain` one does until resolved. Existing failed work remains visible if its assistant is removed. A triggered job has an ID of its own: `bf queue JOB_ID` resolves the job, while `bf queue TRIGGER_ID` shows its trigger, `dispatched` once the job is admitted.

An attempt is recorded when it ends. Ctrl-C or SIGTERM during `bf run`, `bf update`, `bf channel send` or a delivery's `bf queue ID --retry` records it before the process exits. An attempt killed outright, as by the out-of-memory killer, stays `running` or `sending` in `bf queue` and `messaging` until a running watch records it within about a minute, the next `bf update` does, or `bf queue ID --retry` or `--cancel` names it, which needs neither watch nor assistants. The job becomes `uncertain` (`interrupted harness attempt; inspect before retrying`); the delivery follows its channel's [retry policy](#send). While the process still runs, both refuse with `job is still running; retry or cancel it once its attempt ends` or `delivery is still being sent; retry or cancel it once its attempt ends`.

The SQLite queue lives in the private BF state directory under `$XDG_STATE_HOME/bf/runtime/` (normally `~/.local/state/bf/runtime/`), in the folder named by the brain's physical identity (`stat -c %d-%i BRAIN`, or `stat -f %d-%i BRAIN` on macOS), and is bound to the brain's path and name. It is **durable state**, unlike the disposable `.bf/` search cache or run counters. Each commit is durable when it returns (SQLite's `synchronous=EXTRA` in its default rollback-journal mode), so a copy of `queue.sqlite3` taken while no BF process runs is a complete backup. Preserve and back it up with the brain; do not delete it to recover from corruption. A BF process killed while writing leaves a `queue.sqlite3-journal` file beside it, which holds what rolls that write back: keep both. `bf status` then reports `assistant queue holds an interrupted write; run bf queue to recover it`, and `bf queue` or any execution command rolls it back. There is one current queue format, with no automatic migration: a queue another build created is refused, and reported by `bf status`, with `unexpected assistant queue schema; preserve it and repair it`.

Resolve queued work before renaming or moving a brain, or changing its execution configuration. A rename, or a move within one filesystem, keeps the queue: once no job or delivery is unresolved, the next `bf queue` or execution command rebinds it to the new path or name, and `bf status` reads it normally meanwhile. While work admitted under the old path or name remains, those commands refuse, and `bf status` reports as a problem, `assistant queue holds unresolved work admitted under this brain's previous path or name; …`: restore the path or name, resolve the work with `bf queue`, then move or rename the brain again. Collection never waits for the binding, and pending sensor triggers do not hold it. A copy, or a move to another filesystem, gives the brain a new physical identity and an empty queue, leaving the old one in its former folder.

The queue admits at most 10,000 unfinished obligations, counting each job's future deliveries, and caps its database at 256 MiB, of which work may fill 192 MiB. Admitting a message, schedule, trigger or send charges twice its stored size and stops once stored bytes plus the answer space of started jobs reach 96 MiB: `assistant queue is full; finish, retry or cancel unfinished work before accepting more`. At the defaults, a full 1,000-message read batch and hundreds of jobs fit. A read batch that does not fit admits its longest time-ordered prefix, moves the input's progress to the last admitted message and reports `assistant queue is full: admitted N of M messages; …`: the next poll reads the rest. A stream leaves a message it cannot admit unacknowledged, for its provider to redeliver. These checks cannot prevent external disk or filesystem failures.

A job reserves its answer space only when a worker starts it: `max_bytes × (7 + 6 × routes) + (job size + 64 KiB) × (1 + routes)`, where routes are the deliveries the run makes, about 13 MiB at the 1 MiB default with one route. An answered job reserves the same with its saved output in place of `max_bytes`; failed and uncertain jobs reserve nothing. A job that cannot reserve its space stays pending without starting its harness, reported as `assistant queue lacks answer space for this job; it waits until running work finishes`, and starts once running work frees it; `bf run` fails instead and cancels its job. A job needing more than 96 MiB fails with `assistant NAME needs N MiB of queue space per answer, more than one job may reserve; lower its max_bytes or its outputs`; `bf validate` reports such an assistant before any run. Retrying a saved answer reserves its space again or fails with `assistant queue lacks space to record this saved answer; resolve other work first`.

Completed job, event, delivery and conversation bodies each have an 8 MiB retention budget; jobs with unresolved deliveries keep theirs. Identity tombstones, which recognize replays, identical resends and echoes, are deleted after 35 days, longer than Pub/Sub's 31-day redelivery: those of completed or cancelled jobs, delivered or cancelled deliveries with their receipts, lifecycle and input events, and settled triggers. Pending and unresolved work never expires; after those 35 days, a manual delivery ID can be reused. The conversation cache holds at most 10,000 messages. Compaction runs on admission and completion as well as during watch; exact historic job bodies can expire while their evidence records remain.

Queued jobs snapshot their prompt, harness and output configuration; editing an assistant's `schedules`, `triggers`, `inputs` or `keep` leaves them runnable, and once a harness answered, only removing or disabling its assistant or changing an output channel stops its saved answer. A job that no worker tried while its configuration differed runs once you restore it; one a worker tried meanwhile is `failed` with `assistant configuration changed; restore it, then retry or cancel this job with bf queue`, or `output channel configuration changed; …` for a channel's `send` command, and after restoring the configuration it needs `bf queue ID --retry`, or `--cancel` to abandon it. Until then it blocks later jobs of its conversation or schedule. While `bf.yaml` does not load, a starting job waits (`the job waits until bf.yaml loads: …`) instead of failing. A delivery whose channel was removed or whose `send` command changed sends nothing and becomes `uncertain`, still holding its conversation: `channel configuration changed; restore it, then retry or cancel this delivery with bf queue`. Editing a channel therefore cannot silently send old work to a new destination. The guarantee is durable local acceptance and conservative recovery, not universal exactly-once execution: only a provider/adapter idempotency guarantee makes send retries safe.
