# Implement a selected integration

Use once a recurring question and a source are selected and implementation is authorized. Carry forward the selected accounts, folders or repositories, the fields to keep, exclusions, access gaps and the freshness needed. Test with fictional data before any provider runs; no reviewed example is installed in a brain by default.

## Sensors

Start from a reviewed [sensor example](https://github.com/fmind/brain-framework/tree/main/examples/sensors), copied from the release matching `bf --version`, and the [sensor contract](https://fmind.github.io/brain-framework/docs/sensors/). Put the executable in `sensors/` and declare it disabled first, with an explicit account, folder or channel scope and a known behavior for modification times, pagination, partial content and deletions:

```yaml
# https://fmind.github.io/brain-framework/docs/sensors/
sensors:
  docs:
    command: [sensors/local-documents.py, team, "{{home}}/Documents/team"]
    mode: snapshot
    enabled: false # confirm the folder before enabling
    fields:
      kind: { value: document }
```

`command` is direct argv without a shell; arguments accept `{{brain}}`, `{{home}}`, `{{start}}` and `{{end}}`. `bf validate` checks that an enabled program is an executable regular file; check a disabled one with `test -x sensors/NAME`. `mode: window` (the default) collects a time window each run; `mode: snapshot` returns the whole catalog each run, so records it no longer returns are removed. `refresh` sets how often the sensor is due (0 keeps it manual); `priority: low` quiets a high-volume feed on period and home pages.

Shared meanings live once under the top-level `fields:` of `bf.yaml` (description, type, cardinality, `relation: true` for identities, optional `broader` and `targets`); the example assumes `kind` is declared there. A sensor's `fields:` maps each one from a JSON pointer into its output (`path`, such as `/attributes/repository_refs`) or a constant (`value`). Emit explicit namespaced identities only (`person:email/address`, `repo:github.com/owner/name`, lowercase as the reviewed examples write them), never display names or names matched by similarity. Keep the upstream modification time in `attributes.updated` and mark incomplete text with `attributes.partial`; BF sets `attributes.observed`. A mapped identity outside its relation's `targets` fails the run without changing evidence. See [good records](https://fmind.github.io/brain-framework/docs/sensors/#good-records) and the [limits](https://fmind.github.io/brain-framework/docs/limits/).

Test with a fake provider: a complete response and a realistic failure, such as an incomplete page, which must leave saved evidence untouched and keep provider text out of diagnostics. Then, within live authority, set `enabled: true`, preview and collect once:

```bash
bf collect docs --dry-run
bf collect docs
bf read memories/docs
```

`--dry-run` runs the provider and shows samples without saving. An enabled sensor with a `refresh` becomes due for `bf update`, watchers and schedules: recurring execution needs its own authority, so add `refresh: 3600` (seconds) only once it is granted.

After changing a sensor's `fields:` mappings, stored records keep their old projection until collected again. `bf build --reproject SENSOR --dry-run` counts the records that would change, and `bf build --reproject SENSOR` re-applies the current mappings from each record's stored `attributes`, without running the sensor or removing records. A value the sensor never kept in `attributes` needs a new collection.

## Routines

A routine is any deterministic brain program under `routines:`: a review generator, a backup, a check run from a Git hook. It uses no model and makes no decision; it may read the brain with `bf read` and `bf search`, since every program receives `BF_BRAIN` set to its brain. Start from the reviewed [routine example](https://github.com/fmind/brain-framework/tree/main/examples/routines) and the [routine contract](https://fmind.github.io/brain-framework/docs/routines/).

```yaml
# https://fmind.github.io/brain-framework/docs/routines/
routines:
  weekly-review:
    command: [routines/weekly-review.py, "{{brain}}", "{{end}}"]
    refresh: 604800
    output: action
  check-links:
    command: [routines/check-links.sh]
    hooks: [pre-push]
```

- `output: log` (the default) keeps the routine's stdout in `logs/NAME.log`. `output: action` turns non-empty OKF Markdown (`type: action`, `status: draft`) into `actions/YYYY-MM-DD_NAME-XXXXXXXX/ACTION.md`, with an 8-hex suffix and the local date; empty output means nothing to review, and an existing action of that routine for the same day skips the run instead of writing beside it.
- `refresh` makes the routine due in `bf update`, `bf watch` and schedules; without it the routine runs only on demand.
- `hooks` lists events that `bf run --hook EVENT` runs, every enabled routine listing the event in name order.
- A failing routine writes no action, retries after the failure backoff and appears in `bf status` and the home page's `attention`.

Run it on demand, passing extra arguments and piped input through; put `--` before arguments that start with a dash:

```bash
bf run weekly-review --dry-run
bf run weekly-review
bf run --hook pre-push -- origin https://example.test/repo.git
```

`--dry-run` runs the routine but writes no action and records no run; its log still grows. To run hook routines from Git, make `.git/hooks/pre-push` executable with:

```sh
#!/bin/sh
exec bf run --hook pre-push --brain "$HOME/brain" -- "$@"
```

Git passes the remote name and URL as arguments and the pushed refs on stdin; a non-zero exit from any routine blocks the push. Piped input must end within 10 seconds and stay under 1 MiB.

## Verify and hand off

Keep technical checks as fake-provider tests beside the program and questions with expected refs in `evals/`. Run `bf validate` and `bf eval`, then search and read the evidence for the original question. A passing fake-provider test does not prove live account access: report configured, tested and collected sources separately. Ongoing refresh follows [operations](operations.md).
