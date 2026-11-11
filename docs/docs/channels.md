---
icon: lucide/messages-square
description: Connect any messaging surface through passive send, read and stream commands owned by the brain.
---

# Channels

A channel connects your brain to a messaging surface — chat, inbox or desktop notifications — through passive `send`, `read` and `stream` commands declared in `bf.yaml`. BF handles JSON envelopes, durable queueing and retries; your script under `channels/` (or on PATH) owns provider credentials, sender authentication and destination bounds.

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

Each channel defines at least one capability (`send`, `read` or `stream`):

| Setting      | `send` and `read` (finite)                           | `stream` (long-lived)                                          |
| ------------ | ---------------------------------------------------- | -------------------------------------------------------------- |
| `timeout`    | 300s runtime (1–3,600)                               | 30s blocked ACK pipe timeout (1–3,600); idle has no limit      |
| `max_bytes`  | 64 MiB stdout (up to 256 MiB)                        | 1 MiB per UTF-8 frame (1 KiB–1 MiB; emit `ensure_ascii=False`) |
| `idempotent` | `send` only; see [retries](#retries-and-idempotency) | Not available                                                  |

Commands run from the brain root without a shell, expand `{{brain}}`, `{{home}}`, `{{start}}`, `{{end}}` and `{{lines:FILE}}`, receive `BF_BRAIN` with loader-injection variables stripped, and log stderr to `logs/channel.NAME.OPERATION.log` (streams append `== TIME OUTCOME after N.Ns ==` on exit). Copy [`examples/channels/channels/_bf.py`](https://github.com/fmind/brain-framework/blob/main/examples/channels/channels/_bf.py) alongside your channel scripts for `read_envelope()` and `emit_receipt()`.

## Message envelope

`read` and `stream` emit verified, sender-authorized messages in this shape:

```json
{
  "id": "message-42",
  "sender": "owner-id",
  "conversation": "thread-7",
  "time": "2026-10-16T09:00:00Z",
  "text": "What needs my attention?"
}
```

- **Required**: `id` (stable across redelivery), `sender`, `conversation` (scoped per channel), `time`, `text` (<= 32,768 chars; routing fields <= 4,096 chars without control/format chars).
- **Optional**: `reply_to`, `cursor`, `own` (`true` records conversation context without triggering an answer).
- Filter bot echoes in the adapter (BF also drops IDs matching the channel's last 35 days of send receipts). Edits reusing an admitted `id` are ignored; assign a new stable `id` to answer edits.

## Send

`send` reads one JSON delivery request on stdin and prints one JSON receipt on stdout:

```json
{ "id": "stable-delivery-id", "text": "Here is the answer.", "conversation": "thread-7", "reply_to": "message-42" }
```

```json
{ "id": "provider-message-43", "conversation": "thread-7" }
```

Omitted or empty `conversation` targets the endpoint's default destination; receipts must preserve a requested `conversation`. Copy [`desktop.py`](https://github.com/fmind/brain-framework/blob/main/examples/channels/channels/desktop.py) (with [`_bf.py`](https://github.com/fmind/brain-framework/blob/main/examples/channels/channels/_bf.py)) for `notify-send` / `osascript` notifications (note: desktop notifier argv is visible in local process listings). Test directly:

```bash
printf '%s\n' '{"text":"The review is ready."}' | bf channel send desktop --stdin
```

### Retries and idempotency

Set `send.idempotent: true` only when repeating the same delivery `id` and payload cannot post duplicate messages. BF persists the delivery ID for 35 days before calling `send`:

- **Idempotent channel (`send.idempotent: true`)**: failed or interrupted assistant deliveries **and standalone `bf channel send` deliveries** retry automatically in `bf watch` and `bf update` with exponential backoff (2, 4, 8… up to 300s, max 20 attempts) before becoming `failed` (`…; stopped retrying after 20 attempts; retry or cancel it with bf queue`).
- **Default channel (`idempotent: false`)**: failed or interrupted sends become `uncertain` (`interrupted send; inspect before retrying`) and hold their conversation until resolved with `bf queue`.
- **Command never started** (such as missing executable) fails without sending and retries when idempotent; a receipt naming a mismatched `conversation` always becomes `uncertain`.
- **Input job failure notice**: when a channel-originated `input` job enters terminal `failed` or `uncertain` state and a reply channel (`assistant.post` or the input channel's `send`) can send to that thread, BF enqueues one deterministic notice: `[bf] <assistant> did not finish (<state>); run bf queue to inspect.`

## Read

`read` returns one complete `[start, end)` batch of up to 1,000 unique messages (`{"messages": [...]}`), failing rather than returning partial pages:

```bash
bf channel read chat-home --since 2026-10-16T08:00:00Z --until 2026-10-16T10:00:00Z
```

Assistant `mode: read` inputs require `every` seconds, start at activation time, overlap the previous 30 seconds (deduplicating by `id`), and bisect backlogs exceeding 1,000 messages down to 1-second windows (up to 16 parts per poll; >1,000 messages in 1s fails asking for `mode: stream`).

## Stream

`stream` runs a long-lived subprocess under `bf watch`. BF writes a resume frame on stdin:

```json
{ "type": "resume", "since": "2026-10-16T09:00:00Z", "cursor": "" }
```

For each stdout message line, BF commits the event, all subscriber jobs and `cursor` to the durable queue, then writes:

```json
{ "type": "ack", "id": "message-42", "cursor": "" }
```

Acknowledge upstream Pub/Sub or broker messages only after receiving `ack`. BF restarts disconnected streams with 1–60s backoff (resetting after 5s connected) and stops their process groups on exit. Populate `cursor` in polling adapters so reconnects resume from the last admitted message instead of replaying from `since`.

## Queue and recovery

`bf queue` lists job, delivery and wake metadata without message text; `bf queue ID` opens one item's full payload:

```bash
bf queue
bf queue JOB_ID
bf queue JOB_ID --retry
bf queue DELIVERY_ID --retry
bf queue JOB_ID --cancel
```

- `--retry` reuses a saved answer when available, or permits a new harness attempt (inspect side effects first); `--cancel` records tombstones and unblocks the conversation or schedule.
- Crashed processes left `running` or `sending` are recovered by `bf watch`, `bf update` or `bf queue ID --retry|--cancel` (`interrupted harness attempt; inspect before retrying`). Active processes refuse with `job is still running; retry or cancel it once its attempt ends` or `delivery is still being sent; retry or cancel it once its attempt ends`.

### Where the queue lives

`queue.sqlite3` under `$XDG_STATE_HOME/bf/runtime/<dev>-<ino>/` is **durable state** (`synchronous=EXTRA`): back it up while no BF process runs, keep any `-journal` rollback file (`assistant queue holds an interrupted write; run bf queue to recover it`), and never delete it to clear errors. Schema mismatches report `assistant queue predates this release; preserve it and follow the upgrade steps in the changelog` or `unexpected assistant queue schema; preserve it and repair it`.

Capacity and retention bounds:

- Up to 10,000 unfinished obligations, 256 MiB database ceiling (96 MiB admission ceiling: `assistant queue is full; finish, retry or cancel unfinished work before accepting more` or `assistant queue is full: admitted N of M messages; …`).
- Answer reservation (`max_bytes × (7 + 6 × routes) + (job size + 64 KiB) × (1 + routes)`): jobs wait when busy (`assistant queue lacks answer space for this job; it waits until running work finishes`) or refuse saved-answer retries when full (`assistant queue lacks space to record this saved answer; resolve other work first`).
- Completed bodies compact within 8 MiB budgets; identity tombstones last 35 days; unresolved work never expires.

### Move or rename a brain

Drain `bf queue` before moving or renaming a brain:

- Same-filesystem moves/renames rebind automatically once no unresolved work remains; otherwise assistant commands refuse (`assistant queue holds unresolved work admitted under this brain's previous path or name; …`) while collection continues unaffected.
- Device renumbering (such as btrfs across reboots) rebinds by inode and path; ambiguous or unreadable runtime folders report `several assistant queues in the state folder runtime/ are bound to this brain (…)` or `assistant queue in the state folder runtime/ (NAMES) may belong to this brain but cannot be read`.

### Configuration changes

Queued jobs snapshot their prompt, harness and channel settings. Drain `bf queue` before editing `command` or `reply`:

- Editing `schedules`, `inputs` or `keep` leaves queued jobs runnable, and invalid `bf.yaml` pauses starting jobs (`the job waits until bf.yaml loads: …`).
- Changing `command`, `reply` or an output channel's `send` while work is queued blocks affected jobs as `failed` (`assistant configuration changed; restore it, then retry or cancel this job with bf queue` or `output channel configuration changed; …`) and affected deliveries as `uncertain` (`channel configuration changed; restore it, then retry or cancel this delivery with bf queue`) so old work never silently runs new commands or routes to a new destination.
