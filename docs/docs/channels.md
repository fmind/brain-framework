---
icon: lucide/messages-square
description: Connect any messaging surface through passive send, read and stream commands owned by the brain.
---

# Channels

A channel connects your brain to a messaging surface, such as a chat, an inbox or desktop notifications. It is a named endpoint with optional `send`, `read` and `stream` capabilities, each run as a command. Declare it in `bf.yaml`, test it with `bf channel send` or `bf channel read`, then let [assistants](prompts.md) read from it and post to it.

BF understands JSON envelopes and argv, not Google Chat, Slack, email or desktop notifications. The script owns provider access, sender admission, destination restrictions and transport details. Put scripts under `channels/`, or use an executable already on PATH. One script can serve several channel names with different arguments.

Channels have no schedule or polling interval. An assistant chooses its inputs, its `post` channel and its batch-read cadence. A stream adapter implements push, Pub/Sub, long polling or fast polling itself. Its stdin control frames and stdout messages are bidirectional transport control; application replies go through `send`.

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

## Capability settings

A channel needs at least one capability. Each capability has a `command`, and these settings with their defaults:

| Setting      | `send` and `read` (finite)                           | `stream` (long-lived)                                          |
| ------------ | ---------------------------------------------------- | -------------------------------------------------------------- |
| `timeout`    | 300 seconds of runtime, from 1 to 3,600              | 30 seconds for a blocked acknowledgement pipe, from 1 to 3,600 |
| `max_bytes`  | 64 MiB of output, up to 256 MiB                      | 1 MiB per frame, from 1 KiB to 1 MiB                           |
| `idempotent` | `send` only; see [retries](#retries-and-idempotency) | Not available                                                  |

An idle healthy stream has no total runtime deadline. The default frame limit holds the largest valid envelope however it is escaped. Print compact UTF-8 frames, such as Python's `json.dumps(message, ensure_ascii=False)`: a `\uXXXX` escape takes six bytes per character.

Commands run from the brain directory without a shell. `BF_BRAIN` identifies that brain; startup-injection variables are stripped. Stderr goes to bounded `logs/channel.NAME.OPERATION.log`, never the protocol output.

Each stream run ends its log with one `== TIME OUTCOME after N.Ns ==` entry, even when stderr is empty. The outcome is `stopped` or the error. When the adapter ended on its own, `; adapter exited with status N` or `; adapter was killed by SIGNAL` follows.

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

- **Required:** `id`, `sender`, `conversation`, `time` and `text`. IDs must be stable across redelivery.
- **Optional:** `reply_to`, `cursor` and `own` (default false). `own: true` suppresses the bot's output.

The adapter must verify provider authenticity and authorize the sender before emitting. Preserve sender identity; do not label every incoming message as the owner. Conversation identity is scoped by the channel, so another channel's identical thread ID does not share context.

Filter bot echoes in the adapter. BF also drops a message whose ID matches one of that channel's send receipts from the last 35 days, but an echo can arrive before its send returns.

A provider edit that reuses an admitted message's ID keeps the admitted version: the edit starts no job and never stops later messages. To answer edits, give each revision a new stable ID.

Envelope limits:

- IDs and routing fields hold at most 4,096 characters and no control or format characters.
- `text` holds at most 32,768 characters.
- An invalid envelope fails naming the field and the reason, never the value: `channel NAME: invalid stream message envelope: FIELD: REASON`, or `channel NAME: read must return a complete JSON messages batch: messages.N.FIELD: REASON` for a read.

## Send

`send` receives one JSON object on stdin, then exits with one JSON receipt on stdout:

```json
{ "id": "stable-delivery-id", "text": "Here is the answer.", "conversation": "thread-7", "reply_to": "message-42" }
```

```json
{ "id": "provider-message-43", "conversation": "thread-7" }
```

An omitted or empty conversation requests the endpoint's default destination, such as a new thread in one fixed chat. A reply carries its original conversation and message ID. The receipt must preserve the requested conversation.

A desktop adapter can return its own stable notification ID and a fixed conversation identifier. Notification is a send-only channel, not a special BF transport.

Use a configured endpoint directly from the brain directory:

```bash
printf '%s\n' '{"text":"The review is ready."}' | bf channel send desktop --stdin
```

The command creates a delivery ID if omitted, saves the obligation, attempts that delivery and prints its state and receipt. A non-delivered result exits 1; the [messaging example](https://github.com/fmind/brain-framework/tree/main/examples/channels) shows a failed send and its retry.

A provider length limit should fail visibly and leave the saved answer intact: silently truncating answers can remove essential qualifications.

### Retries and idempotency

Declare `idempotent: true` only when repeating an identical delivery ID and payload cannot create another visible message. Use the provider's idempotency mechanism or a durable adapter implementation.

BF persists the delivery ID before calling send and reuses it on retry. For [35 days](#queue-and-recovery), sending that ID again returns the saved delivery when the content is identical and fails when it differs. A deliberately new send needs a new ID.

What happens when a send fails or is interrupted:

- **Assistant delivery, idempotent channel:** it retries on its own after 2, 4, 8… seconds, at most 300 seconds apart. After 20 attempts it becomes `failed` (`…; stopped retrying after 20 attempts; retry or cancel it with bf queue`).
- **Assistant delivery, default channel:** it becomes `uncertain` and requires inspection (`interrupted send; inspect before retrying`).
- **Standalone `bf channel send`:** watch and `bf update` never send or retry it. It becomes `failed` on an idempotent channel (`…; retry it with bf queue ID --retry`) and `uncertain` otherwise.
- **Send command that never started,** such as one not on PATH: it sent nothing, so it fails or retries like an idempotent one. A harness that never started leaves its job `failed`, not `uncertain`.

An explicit `bf queue ID --retry` starts the attempts over. Saved answers are never regenerated to retry delivery.

## Read

`read` is a finite, complete `[start,end)` batch. BF expands `{{start}}` and `{{end}}` in argv. The script must finish all provider pagination and print one object:

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

A batch holds at most 1,000 unique messages, all inside its requested window. Fail rather than return a partial successful batch. Incomplete results, duplicate IDs, extra pagination fields and out-of-window messages fail without advancing input progress.

Test a read by hand:

```bash
bf channel read chat-home --since 2026-10-16T08:00:00Z --until 2026-10-16T10:00:00Z
```

Manual reads invoke no assistant and move no subscription checkpoint. A manual `bf channel read` of a window holding more than 1,000 messages fails: split it into smaller windows.

### Read inputs

An assistant `mode: read` input requires `every` seconds. Its first activation starts at the current time. Later calls overlap the last 30 seconds to catch modest provider indexing delays, and deduplicate by message ID.

This overlap is a bounded lateness allowance, not a guarantee for arbitrary delayed history. Use a durable stream for reliable provider event delivery, or fetch older history explicitly.

### Catch up on a backlog

A read input whose window holds more than 1,000 messages catches up in parts:

- It reads again from its checkpoint, without the overlap, halving the window until a part fits.
- It admits that part and moves its checkpoint to the part's end. The next part is twice as long, up to the end of the poll's window.
- A poll admits at most 16 parts. Halving does not count, since it ends at one second.
- While a backlog remains, watch polls the input again about a second later instead of after `every`. `bf update` continues at its next run.
- More than 1,000 messages within one second fail with `channel NAME: more than 1000 messages fall within one second; read them with a stream input`. Every part admitted before is kept.

## Stream

A stream is a long-lived subprocess. BF writes a resume frame on stdin:

```json
{ "type": "resume", "since": "2026-10-16T09:00:00Z", "cursor": "" }
```

The adapter emits one message envelope per stdout line and flushes. BF commits the message, every configured recipient and its transport checkpoint to the local queue, then sends:

```json
{ "type": "ack", "id": "message-42", "cursor": "" }
```

Only then should a Pub/Sub adapter acknowledge the provider message. ACK means durable acceptance, not a completed model answer. On process failure, unacknowledged input must be redelivered with the same ID.

### Resume position

- A cursor is opaque to BF. Preserve the provider's resume and ack semantics, and advance the cursor only through accepted frames.
- `since`, which is also `{{start}}` in a stream command, is the stream's first start and never advances.
- A polling adapter resumes from `cursor`. It puts its provider position in each envelope's `cursor`, and each later resume frame carries the cursor of the last admitted message.
- An adapter that leaves `cursor` empty replays everything since that first start on every reconnect. A replayed message whose 35-day identity tombstone has expired is answered again.
- A push adapter can drain its retained backlog; BF does not discard older pushed messages by timestamp.

### Adapter duties and restarts

- Read control frames continuously, or emit one message and wait for its ACK. BF bounds frame and ACK buffers.
- Provider retry, subscription renewal and acknowledgement-deadline extension belong to the adapter.
- BF restarts failed transports with 1–60-second backoff and kills their process groups when watch exits.
- An adapter still running 5 seconds after BF wrote its resume frame counts as connected. Its earlier transport error clears from `bf status`, and the backoff restarts at 1 second.
- An adapter that exits sooner keeps its error. So does a refused admission, such as a full queue, until a message is admitted again.
- Do not run two consumers against the same exclusive provider subscription. BF owns one stream per used channel in one brain, not across separate brains.

## Queue and recovery

BF keeps assistant jobs and channel deliveries in a durable local queue. The guarantee is durable local acceptance and conservative recovery, not universal exactly-once execution: only a provider or adapter idempotency guarantee makes send retries safe.

`bf queue` lists job and delivery identities, states and errors without message bodies. `bf queue ID` explicitly opens a job or delivery, including private input and output.

```bash
bf queue
bf queue JOB_ID
bf queue JOB_ID --retry
bf queue DELIVERY_ID --retry
bf queue JOB_ID --cancel
```

`bf status --check` fails while any of these remains. `attention` names each, and `messaging` holds the details, as [status](commands.md#status-sources-and-routines) describes:

- an assistant that is not disabled has a failed or uncertain job, or schedules that cannot start;
- a channel has an unresolved delivery that failed or is uncertain;
- a channel has a current stream or read error.

### Retry, cancel and hold

- A job retry resumes its saved answer or preparation when available. Otherwise it permits another harness attempt, which can repeat external tool effects: inspect first.
- A delivery retry immediately attempts only that saved delivery, so standalone sends need no assistant or watcher.
- Cancellation retains identity tombstones and unblocks subsequent work. Cancelling a scheduled job skips its window.
- A `failed` delivery no longer holds later work of its conversation or schedule; an `uncertain` one does until resolved.
- Failed work remains visible if its assistant is removed, and keeps `bf status --check` failing until resolved. A disabled assistant's work stays listed in `bf queue` without failing it.

### Interrupted attempts

An attempt is recorded when it ends. Ctrl-C or SIGTERM during `bf run`, `bf update`, `bf channel send` or a delivery's `bf queue ID --retry` records it before the process exits.

An attempt killed outright, as by the out-of-memory killer, stays `running` or `sending` in `bf queue` and `messaging`. It is recorded by the first of:

- a running watch, within about a minute;
- the next `bf update`;
- `bf queue ID --retry` or `--cancel` naming it, which needs neither watch nor assistants.

The job then becomes `uncertain` (`interrupted harness attempt; inspect before retrying`); the delivery follows its channel's [retry policy](#retries-and-idempotency). While the process still runs, retry and cancel refuse with `job is still running; retry or cancel it once its attempt ends` or `delivery is still being sent; retry or cancel it once its attempt ends`.

### Where the queue lives

The SQLite queue is **durable state**, unlike the disposable `.bf/` search cache or run counters. Preserve and back it up with the brain; do not delete it to recover from corruption.

- **Location:** the private BF state directory under `$XDG_STATE_HOME/bf/runtime/` (normally `~/.local/state/bf/runtime/`), in the folder named by the brain's physical identity: `stat -L -c %d-%i BRAIN`, or `stat -L -f %d-%i BRAIN` on macOS. The queue is bound to the brain's path and name.
- **Backups:** each commit is durable when it returns (SQLite's `synchronous=EXTRA` in its default rollback-journal mode). A copy of `queue.sqlite3` taken while no BF process runs is therefore a complete backup.
- **Interrupted writes:** a BF process killed while writing leaves a `queue.sqlite3-journal` file beside it, which holds what rolls that write back: keep both. `bf status` then reports `assistant queue holds an interrupted write; run bf queue to recover it`, and `bf queue` or any execution command rolls it back.
- **Format:** there is one current queue format, with no automatic migration. `bf status` reports a refused queue. One from an earlier release reads `assistant queue predates this release; preserve it and follow the upgrade steps in the changelog`. One another build created reads `unexpected assistant queue schema; preserve it and repair it`.

### Move or rename a brain

Resolve queued work before renaming or moving a brain, or changing its execution configuration.

- A rename, or a move within one filesystem, keeps the queue. Once no job or delivery is unresolved, the next `bf queue` or execution command rebinds it to the new path or name. `bf status` reads it normally meanwhile.
- While work admitted under the old path or name remains, those commands refuse. `bf status` reports it as a problem: `assistant queue holds unresolved work admitted under this brain's previous path or name; …`. Restore the path or name, resolve the work with `bf queue`, then move or rename the brain again.
- Collection never uses the queue, so it never waits for the binding.
- A copy, or a move to another filesystem, gives the brain a new physical identity and an empty queue. The old queue stays in its former folder.
- A new device number for the same folder, such as btrfs numbering a device anew at boot, keeps the queue and, with it, the programs' private state. The next BF command that needs them renames the folder with the same inode whose queue is bound to the brain's path; a brain without a queue keeps no program state, so window sensors restart from `lookback`. The same path and inode on another disk, such as a cloned disk swapped in, takes the queue too.
- When several such folders are bound to the brain, assistant commands refuse with `several assistant queues in the state folder runtime/ are bound to this brain (…)`, and when one cannot be read, such as after an interrupted write, with `assistant queue in the state folder runtime/ (NAMES) may belong to this brain but cannot be read`. Rename the right folder as the error says and move the others out. Collection and watch keep running meanwhile.

### Capacity

| Limit                  | Value                                                                |
| ---------------------- | -------------------------------------------------------------------- |
| Unfinished obligations | 10,000, counting each job's future deliveries                        |
| Database size          | 256 MiB, of which work may fill 192 MiB                              |
| Admission              | Stops once stored bytes plus started jobs' answer space reach 96 MiB |

- Admitting a message, schedule or send charges twice its stored size. A refused admission reads `assistant queue is full; finish, retry or cancel unfinished work before accepting more`.
- At the defaults, a full 1,000-message read batch and hundreds of jobs fit.
- A read batch that does not fit admits its longest time-ordered prefix and moves the input's progress to the last admitted message. It reports `assistant queue is full: admitted N of M messages; …`, and the next poll reads the rest.
- A stream leaves a message it cannot admit unacknowledged, for its provider to redeliver.
- These checks cannot prevent external disk or filesystem failures.

### Answer space

A job reserves its answer space only when a worker starts it: `max_bytes × (7 + 6 × routes) + (job size + 64 KiB) × (1 + routes)`. Routes are the deliveries the run makes: one when it answers a message or posts, none otherwise. This is about 13 MiB at the 1 MiB default with one route.

- An answered job reserves the same with its saved output in place of `max_bytes`. Failed and uncertain jobs reserve nothing.
- A job that cannot reserve its space stays pending without starting its harness, reported as `assistant queue lacks answer space for this job; it waits until running work finishes`. It starts once running work frees the space. `bf run` fails instead and cancels its job.
- A job needing more than 96 MiB fails with `assistant NAME needs N MiB of queue space per answer, more than one job may reserve; lower its max_bytes`. With one route and the 4 MiB `max_bytes` limit, no job needs that much.
- Retrying a saved answer reserves its space again, or fails with `assistant queue lacks space to record this saved answer; resolve other work first`.

### Retention

- Completed job, event, delivery and conversation bodies each have an 8 MiB retention budget. Jobs with unresolved deliveries keep theirs.
- Identity tombstones recognize replays, identical resends and echoes. They are deleted after 35 days, longer than Pub/Sub's 31-day redelivery. They cover completed or cancelled jobs, delivered or cancelled deliveries with their receipts, and lifecycle and input events.
- Pending and unresolved work never expires. After those 35 days, a manual delivery ID can be reused.
- The conversation cache holds at most 10,000 messages.
- Compaction runs on admission and completion as well as during watch. Exact historic job bodies can expire while their evidence records remain.

### Configuration changes

Queued jobs snapshot their prompt, harness and output configuration. Do not swap the `command` or `text` of an assistant that has pending work, even for a moment: work queued meanwhile keeps the changed settings, runs with them and delivers whatever that command prints, or fails once you restore them.

- Editing an assistant's `schedules`, `inputs` or `keep` leaves its jobs runnable.
- Once a harness answered, only removing or disabling its assistant or changing an output channel stops its saved answer.
- A job that no worker tried while its configuration differed runs once you restore it.
- A job a worker tried meanwhile is `failed` with `assistant configuration changed; restore it, then retry or cancel this job with bf queue`, or `output channel configuration changed; …` for a channel's `send` command. After restoring the configuration, it needs `bf queue ID --retry`, or `--cancel` to abandon it. Until then it blocks later jobs of its conversation or schedule.
- While `bf.yaml` does not load, a starting job waits (`the job waits until bf.yaml loads: …`) instead of failing.
- A delivery whose channel was removed or whose `send` command changed sends nothing and becomes `uncertain`, still holding its conversation: `channel configuration changed; restore it, then retry or cancel this delivery with bf queue`. Editing a channel therefore cannot silently send old work to a new destination.
