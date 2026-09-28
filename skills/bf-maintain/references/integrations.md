# Implement a selected integration

Use after setup or discovery has identified a recurring question and implementation is authorized. Carry forward the selected accounts, folders or repositories, retained fields, exclusions, access gaps and freshness requirement. Test with fictional data before enabling provider execution. No reviewed example is assumed to be installed in a brain.

## Sensors

Start from a reviewed [sensor example](https://github.com/fmind/brain-framework/tree/main/examples/sensors) and the [sensor contract](https://fmind.github.io/brain-framework/docs/sensors/). Keep title/text focused on searchable evidence. Declare the sensor disabled with explicit account/folder/channel scope, modification time, pagination, partial-content and deletion behavior.

Test a complete response and a realistic failure, such as an incomplete provider page; failure must preserve saved evidence. Treat provider output as data and keep diagnostics private. Declare `refresh` in `bf.yaml` for the agreed freshness requirement.

Shared field meanings, types, cardinality and examples live under `schema` in `bf.yaml`; sensor `fields` map explicit output paths or constants into them. Sensors emit explicit namespaced identities only (`person:email/address`, `repo:github.com/owner/name`, lowercase as the example sensors write them: identities are case-sensitive), never display names or names matched by similarity. Map provider fields explicitly: `attributes.updated` holds the upstream modification time and `attributes.partial` marks incomplete text, while BF sets `attributes.observed`. Keep ids within 7,988 characters once percent-encoded and URLs within 8,192 characters without control characters. See [good records](https://fmind.github.io/brain-framework/docs/sensors/#good-records) and the [limits](https://fmind.github.io/brain-framework/docs/limits/#size-bounds).

For a runnable first check, follow the [local-file sensor example](https://fmind.github.io/brain-framework/docs/getting-started/#collect-your-first-source). Expect its fictional record, the mapped field and the source link after collection. For a live sensor, reuse existing execution authority or obtain the missing authority after preparing disabled configuration and passing fake-provider tests. `bf collect SENSOR --dry-run` executes the provider but does not save records; it can still write private stderr logs.

## Routines

Start from a reviewed [routine example](https://github.com/fmind/brain-framework/tree/main/examples/routines) and the [routine contract](https://fmind.github.io/brain-framework/docs/routines/). Copy the chosen script into `routines/`, test with a fake `bf`, and declare it under `routines:` with a `refresh`.

Keep it deterministic: read `bf read`/`bf search` pages and print OKF Markdown with `type: action` and `status: draft`, or nothing when there is nothing to review. No model, network or provider call belongs in the routine. `bf update` validates output and writes `actions/YYYY-MM-DD_NAME-UUID/ACTION.md` after the sensors without replacing an action. A failing routine writes no action, retries after the failure backoff (1 minute, doubling up to its `refresh`) and appears under home-page `attention`.

Preview a reviewed script directly in a disposable brain and verify its valid action or intentional empty result. This is execution; `bf update --dry-run` only plans and cannot establish output validity.

## Verify and hand off

Keep technical checks in `tests/` and questions with expected refs in `evals/`. Run `bf validate` and `bf eval`, then search and read the evidence for the original question. A passing fake-provider test does not prove live account access. Report configured, tested and actually collected sources separately. Ongoing refresh follows [operations](operations.md); enabling a sensor does not authorize a new schedule.
