---
icon: lucide/gauge
description: Look up size, page and process limits, and the safeguards around running programs.
---

# Limits and safeguards

Exact resource, pagination and subprocess bounds enforced by Brain Framework. See [Troubleshooting](troubleshooting.md) for recovery and [Privacy](privacy.md) for trust boundaries.

## Size bounds

| Resource                                | Limit                                                                                                                                                                                                        |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bf.yaml`, machine registry, eval suite | 1 MiB each; YAML <= 32 nesting levels, 20,000 events, 4,300-digit integers                                                                                                                                   |
| Authored note                           | 4 MiB; path <= 7,988 chars percent-encoded                                                                                                                                                                   |
| Note or record title                    | 4,096 characters (listings preview 200 chars + `…`)                                                                                                                                                          |
| Note `aliases`, `links`, `tags`         | 1,000 each; `entity`, `resource`, alias, `links` and `sources.resource` <= 8,192 chars; tag label 1–128 chars                                                                                                |
| Search or page reply                    | Soft page cut near 32 KiB (`next_offset`); hard reply ceiling 4 MiB (`response exceeds its byte limit; narrow the request`)                                                                                  |
| Paged exact read                        | ~32 KiB text slices (`4 KiB` + up to 200-entry `outline` on page 1 of multi-section notes)                                                                                                                   |
| Graph previews                          | Backlinks: 5 items/relation (160-char excerpt), 64 relation groups (`backlinks_omitted`); claims: 20/relation, 50 total (`claims_truncated`)                                                                 |
| Busy writer waits & source settle       | 2s cache wait (3s exact record wait) before serving `stale`; up to 120s when no cache exists; 5s `memories/SOURCE` settle rescan                                                                             |
| Record file & metadata                  | 16 MiB file; non-`text` metadata (`relations`, `attributes`) <= 2 MiB UTF-8 JSON; `id` 1–4,096 chars (<= 7,988 percent-encoded)                                                                              |
| Record `text`                           | 4,194,304 bytes/chars; longer text fails unless sensor sets `max_text_bytes` (1–4,194,304), cutting on UTF-8 boundary with `attributes.partial: true`                                                        |
| Record `links`, `aliases`, `relations`  | 1,000 links/aliases/relation targets each; URL, link or identity <= 8,192 chars (listings show <= 200 chars)                                                                                                 |
| Directory scan & depth                  | 100,000 entries per source or authored folder (`bf status` warns above 80,000); <= 64 directory levels                                                                                                       |
| Search query & page sizes               | 4,096 chars, first 32 content words, up to 64 stems per `word*` prefix; search 1–50; folders/tags 200; `memories/SOURCE` 20; other pages 50                                                                  |
| Problem & diagnostic caps               | 200 skipped files, validation problems, warnings or unresolved targets (`*_truncated`); 5 reasons per diagnostic; offsets <= `2^53 - 1`                                                                      |
| Relation `targets` & evaluation suites  | 64 prefixes (<= 1,024 chars each); 100 eval suites/run, 200 cases/suite                                                                                                                                      |
| Collection & reprojection               | 99,999 changed records/collection; `collect` stdout 64 MiB default (up to 256 MiB); reprojection 1,000 records/transaction                                                                                   |
| Job, piped stdin & stream output        | Job stdout 64 MiB default (1 B–256 MiB); piped `--stdin` <= 1 MiB within 10s; stream frame 64 MiB default (reads newline-delimited frames)                                                                   |
| Prompts, names & commands               | Prompt file and expanded prompt <= 120,000 bytes; job name <= 64 chars; command + up to 127 args (<= 16,384 chars/line, expanded <= 128 KiB/arg)                                                             |
| Cadence, retention & logs               | `timeout` 1–3,600s (default `300s`); `every`/`lookback`/`overlap`/`reconcile`/`silent_after` <= 365d; `expire_after` 1h–100y; `.forgotten` <= 8 MiB; `keep` 1–50,000; `logs/NAME.log` <= 1 MiB (256 KiB/run) |

## Processes and logs

Subprocesses run from the brain root without a shell, with `BF_BRAIN` and any `requires.env` variables set, stdin `/dev/null` (unless `--stdin`, hook stdin or prompt stdin), and loader/startup injection variables stripped (`LD_*`, `DYLD_*`, `GCONV_PATH`, `BASH_ENV`, `ENV`, `BASH_FUNC_*`, `SHELLOPTS`, `BASHOPTS`, `PS4`, `ZDOTDIR`, `PYTHON*`, Java/Node/Ruby/Perl/PHP/.NET/R/Lua startup hooks, and empty/relative `PATH` entries).

- **Termination**: BF kills the entire process group when a run ends, times out or exceeds `max_bytes` — sending `SIGTERM`, waiting up to 5 seconds, then sending `SIGKILL` (`bf watch` and service units allow 60 seconds on shutdown for transaction rollback).
- **Logs (`logs/NAME.log`)**: gitignored, unindexed, bounded to newest whole entries within 1 MiB; never quoted in diagnostics.
- **Usage (`.bf/usage.sqlite3`)**: stores daily aggregate counts by operation and kind (plus <= 8 unmatched tokens per empty search; capped at 1 MiB, rows dropped after 30 days) — never queries, refs, paths or identities.
  - `search` kinds: `none`, `period`, `folder`, `note`, `memories`, `source`, `source-period`, `record`, `identity`, `tag`, `combined`.
  - `read` kinds: `home`, `review`, `period`, `tasks`, `tags`, `tag`, `memories`, `source`, `source-period`, `record`, `folder`, `note`, `section`, `action`, `identity`, `relation`.

Unified jobs and durable queue bounds ([channel recovery](channels.md#queue-and-recovery)):

| Resource                    | Limit                                                                                                                    |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Jobs, triggers & `requires` | <= 128 jobs/brain; <= 32 triggers (`on`) per job; <= 64 `bins` and <= 64 `env` entries in `requires`                     |
| Retries & deliveries        | `retries` `0`–`20` (`send:` with `idempotent: true` retries with backoff up to `20` attempts and 300s delay)             |
| Messages & `send:`          | <= 1,000 messages per `emit: messages` batch; outgoing `send:` `text` <= 4 MiB                                           |
| Queue capacity              | 256 MiB DB ceiling; admission/enqueue enforces `10,000` unfinished deliveries+wakes (`MAX_PENDING`) and 96 MiB ceiling   |
| Wakes                       | Up to `16` active pending wakes total and wake recursion depth <= `8`; positive delay/instant required                   |
| Retention                   | Completed deliveries, runs, events, wakes and receipts kept 35 days; newest 8 MiB kept for event/delivery/message bodies |
