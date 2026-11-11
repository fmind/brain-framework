# Extension recipes: sensors, routines, assistants and channels

Use once a source or automation is approved; test with synthetic fixtures before running against live providers or models. Run `bf skills skills --link` inside the brain root to link the four packaged `bf-*` skills into `.agents/skills` and `.claude/skills`, and symlink any custom `skills/<name>` folders there directly.

## Sensors (`_sensor.py`)

Copy [`examples/sensors/_sensor.py`](https://github.com/fmind/brain-framework/blob/main/examples/sensors/_sensor.py) (`cli` runner, `BF_START`/`BF_END` window parsing, `load_state`/`save_state` in `BF_STATE_DIR`, `git_remote_identity`) beside your script in `sensors/` and start disabled ([sensor contract](https://fmind.github.io/brain-framework/docs/sensors/)):

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  docs:
    command: [sensors/local-documents.py, team, "{{home}}/Documents/team"]
    mode: snapshot
    max_text_bytes: 262144
    enabled: false
    fields:
      kind: { value: document }
```

- Sensors print a JSON array or JSON Lines (JSONL) of records (`id`, `title`, optional `text`, `time`, `url`, `links`, `aliases`, `attributes`).
- Long text fails unless the sensor sets `max_text_bytes`, which cuts UTF-8 cleanly and sets `attributes.partial: true`.
- Emit [standard vocabulary](https://fmind.github.io/brain-framework/docs/schema/#standard-vocabulary) attributes (`author_refs`, `sender_refs`, `repository_refs`, `status`, `kind`, `updated`) and lowercase `scheme:value` identities; follow [credentials](https://fmind.github.io/brain-framework/docs/sensors/#credentials) and clean temp copies on `SIGTERM` ([limits](https://fmind.github.io/brain-framework/docs/limits/#processes-and-logs)).
- Test offline with `bf collect docs --input fixture.jsonl`, then within live authority enable and run `bf collect docs --dry-run`, `bf collect docs` and `bf read memories/docs`; re-apply changed mappings offline with `bf build --reproject docs --dry-run` / `bf build --reproject docs`.

## Routines, declarative `when:` and `_bf.py`

Copy [`examples/routines/_bf.py`](https://github.com/fmind/brain-framework/blob/main/examples/routines/_bf.py) (`window`, `state_dir`, `load_state`/`save_state`, `bf_json`, `bf_pages`, `bf_export`, `visible`/`plain`/`code`, `emit_events`, `emit_action`, `read_envelope`/`emit_receipt`) into `routines/` ([routines](https://fmind.github.io/brain-framework/docs/routines/)). Stream records via `bf export --kind items --scope memories/SOURCE --observed-since 7d` (includes `fields`, multi-value `values`, `attributes` and `text`) instead of reading `memories/` files directly:

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    at: "08:00"
    days: [mon]
    timezone: Europe/Paris
    when: { review: true, actions: true }
    output: action
  check-links:
    command: [routines/check-links.sh]
    hooks: [pre-push]
```

- Schedule routines by interval (`refresh: 604800`) **or** wall clock (`at`, optional `days` and `timezone` — mutually exclusive with `refresh`).
- Optional `when:` (`sources: [NAME]`, `review: true`, `actions: true`, `attention: true`) evaluates offline against SQLite/health and skips as `gated` without spawning a subprocess when nothing matched.
- Run manually via `bf run weekly-review --dry-run`, `bf run weekly-review` or `bf run --hook pre-push -- origin https://example.test/repo.git < /dev/null`. Wire `.git/hooks/pre-push` as `exec bf run --hook pre-push --brain "$HOME/brain" -- "$@"` (prefix with `exec uv run --project "$HOME/brain" --locked bf` in a reviewed pinned brain).

## Assistants and channels {#assistants-and-channels}

Assistants run the owner's harness over `prompts/NAME.md` ([assistant guide](https://fmind.github.io/brain-framework/docs/prompts/), [channels](https://fmind.github.io/brain-framework/docs/channels/)). Keep base `command` read-only and scope extra flags per trigger with `input_args`, `schedule_args` and per-schedule `args` instead of wrapper scripts:

```yaml
# https://fmind.github.io/brain-framework/docs/prompts/
assistants:
  triage:
    prompt: prompts/triage.md
    command:
      [
        claude,
        --print,
        --output-format,
        json,
        --json-schema,
        "{{schema}}",
        --tools,
        "Read,Grep,Glob,Bash",
        --allowedTools,
        "Bash(bf read *),Bash(bf search *)",
        --permission-mode,
        dontAsk,
      ]
    input_args: [--allowedTools, "Bash(bf action *)"]
    reply: { path: /structured_output }
    propose: true
    idempotent: [schedule, wake]
    inputs: [desk]
    post: desk
    schedules:
      inbox:
        every: 1800
        when: { sources: [mail] }
      consolidate:
        at: "18:00"
        days: [fri]
        when: { review: true }
```

- **Schedules & `when:`**: each schedule sets `every` **or** `at` (never both). `when:` runs offline first and populates `{{context}}`'s `events` (up to 64 events / 64 KiB); if custom `gate:` ([records-gate.py](https://github.com/fmind/brain-framework/blob/main/examples/routines/records-gate.py)) is also set, `gate:` runs only after `when:` matches.
- **Safe action proposals (`propose: true`)**: keeps the harness read-only while letting `assistant-reply` return up to 3 `actions` (`{slug, markdown}`), validated and saved strictly as `status: draft` `actions/YYYY-MM-DD_SLUG/ACTION.md` notes ([propose, then act](https://fmind.github.io/brain-framework/docs/prompts/#propose-then-act)).
- **Channels**: implement `send`, `read` or `stream` in `channels/` using `_bf.py` (`read_envelope`, `emit_receipt`); authenticate senders in the adapter and admit an input channel only when every admitted sender may see what the harness reads ([choose inputs](https://fmind.github.io/brain-framework/docs/prompts/#choose-inputs)).
- **Retries, wakes & recovery**: `retries` (`0..20`, default `3` when `idempotent`, else `0`) governs automatic retries; harness exit `75` (`EX_TEMPFAIL`) retries with backoff without entering `uncertain`. Terminal `failed` (never `uncertain`) `schedule:<name>` jobs self-heal at the next due window, standalone `bf channel send` retries in `bf watch` when `send.idempotent: true`, per-slot wakes (`wake:{assistant}:{slot}`, up to 16, with `reason` in `bf queue`) isolate conversations from schedules, and terminal `failed`/`uncertain` channel inputs post one deterministic `[bf] <assistant> did not finish (<state>); run bf queue to inspect.` notice.

Verify with `bf validate` and `bf eval`, reporting offline checks, fixture runs and live runs separately; ongoing operations live in [operations.md](operations.md).
