# Runnable example brain

A fictional project, OKF concepts, resumable actions, a decision review and a local sensor demonstrate the complete workflow without credentials or network access. Run this from the Brain Framework checkout after `uv sync --locked`:

```bash
bf_checkout=$PWD
bf_demo=$(mktemp -d)
bf_demo=$(cd "$bf_demo" && pwd -P)
bf_example="$bf_demo/brain"
mkdir "$bf_example"
cp -R examples/brain/. "$bf_example"
cd "$bf_example"
bf() {
  env -u BF_BRAIN XDG_CONFIG_HOME="$bf_demo/config" XDG_STATE_HOME="$bf_demo/state" \
    uv run --project "$bf_checkout" bf "$@"
}
bf update --dry-run
bf update
bf read
bf search retention
bf read demo:retention
bf read actions/2026-09-19_retention
bf validate
bf eval
```

On this disposable copy, expect `bf validate` to return `"valid":true` and `bf eval` to return `"passed":true` with `"score":"16/16"`. Reading `demo:retention` gives the fictional evidence that upstream content can disappear; the action read lists `ACTION.md`, its input request and, under `backlinks`, the project that links to it.

`update` runs the configured `sensors/demo.py` in this copy; the wrapper keeps the demo's configuration and state in its temporary directory. A second update within the hour runs nothing. The fake sensor emits one fictional event inside each requested window, always with the same id, so later collections update the same JSON record file in `memories/demo/`.

`bf read` shows the example project with its first open task, and `bf read actions/2026-09-19_retention` returns the action with its request file and the project that links to it. Continue that action by writing its answer with the record ref, then find it again:

```bash
cat > actions/2026-09-19_retention/outputs/answer.md <<'EOF'
# Answer

Keep originals because upstream content can disappear. Evidence: [record](demo:retention).
EOF
bf search "keep originals" --scope actions
bf validate
```

Then tick the action's tasks in `ACTION.md`, fill its Outcome and record any next step in Resume. Use `status: stable` when the note is reviewed and ready to use; task completion and note maturity are separate. For real data, create your own brain with `bf init ~/brain` (or another chosen path) and copy the patterns you need. See the [brain layout](../../docs/docs/brain.md).

## Follow an explicit link

```bash
bf read bf://example/concepts/retention.md
bf read bf://example/projects/example
bf read 'bf://example/projects/example.md#now'
```

Read the concept's `backlinks` to find the project under `related-to`. Read the project's `claims` to find the same link and its origin, `projects/example.md#now`. These are two views of one declared relationship. `bf read repo:example/project` also groups the collected record under its `repository` role; `bf.yaml` restricts that role to `repo:` identities with `targets`, so a sensor mapping any other value fails without saving. Use real identities when adapting the example.

`bf.yaml` also declares `depends-on` with `broader: related-to`, so a `related-to` role page lists dependencies too:

```bash
bf read bf://example/concepts/archive-policy.md --rel related-to
```

It returns `"total":1` with the decision `actions/2026-09-25_retention-review/outputs/decision.md`, whose item keeps its own `"relation":"depends-on"`. The policy's backlinks still group that link under `depends-on`.

## Export the graph

Print every claim as one JSON object per line, for tools such as DuckDB or networkx:

```bash
bf export edges | head -3
```

With `TZ=UTC`, the first three lines are:

```text
{"brain":"example","origin":"bf://example/actions/2026-09-19_retention/ACTION.md","relation":"tagged-with","subject":"bf://example/actions/2026-09-19_retention/ACTION.md","target":"bf://example/tags/retention"}
{"brain":"example","origin":"bf://example/actions/2026-09-25_retention-review/ACTION.md#context","relation":"links","subject":"bf://example/actions/2026-09-25_retention-review/ACTION.md","target":"bf://example/actions/2026-09-25_retention-review/outputs/decision.md","time":"2026-09-25T00:00:00.000000Z"}
{"brain":"example","origin":"bf://example/actions/2026-09-25_retention-review/ACTION.md#decision","relation":"links","subject":"bf://example/actions/2026-09-25_retention-review/ACTION.md","target":"bf://example/actions/2026-09-25_retention-review/outputs/decision.md","time":"2026-09-25T00:00:00.000000Z"}
```

Lines are sorted by subject, relation, target and origin. The first action has no `updated` date, so its claim has no `time`; a note's time is local midnight of its date, so another timezone shifts it. `head` closing the pipe early is expected. See [export edges](../../docs/docs/commands.md#export-the-graph).

## Review a decision

All dates, policies and outcomes in these notes are fictional; the draft procedure asserts no real-world verification. `bf.yaml` declares the `depends-on` and `supersedes` roles the notes link with.

```bash
bf read 'actions/2026-09-25_retention-review/ACTION.md#context'
bf read 'actions/2026-09-25_retention-review/ACTION.md#resume'
bf read 'bf://example/concepts/archive-policy.md#retention'
bf read bf://example/concepts/archive-policy.md
bf read actions/2026-09-25_retention-review/outputs/prior-decision.md
bf read projects/example.md#intention
bf read projects/example.md#unknown
bf read concepts/selected-evidence.md
```

| Workflow        | Where it lives                                                  | What to notice                                                                             |
| --------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Working context | `actions/2026-09-25_retention-review/ACTION.md#context`         | The next step fits in a few sentences and six refs, without a transcript.                  |
| Dependency      | `concepts/archive-policy.md`, read whole                        | Its `depends-on` backlink names the decision to review when the policy changes.            |
| Supersession    | `actions/2026-09-25_retention-review/outputs/prior-decision.md` | The old decision keeps its failed prediction and a `supersedes` backlink from the new one. |
| Intention       | `projects/example.md#intention`                                 | It waits for a policy change and names its owner, condition and expiry.                    |
| Unknown         | `projects/example.md#unknown`                                   | The storage-cost question names the observation that would resolve it.                     |
| Draft procedure | `concepts/selected-evidence.md`                                 | It stays draft until tried on a separate case.                                             |

These reads expose the evidence; an agent or person performs the review. The example project uses `review_after: 7`: its reminder becomes due seven days after the local file edit, independently of its authored timeline date. Add an explicit `review_due` date when a deadline must survive copying or a Git checkout. `bf read tasks` lists the open project, concept and canonical-action checkboxes with source sections and full-selection counts; it excludes action attachments and deprecated notes.

### Retain and compare the policy

Retain the exact policy section locally without inserting its body into an agent message. This block runs in a subshell, writes only to the disposable example with owner-only permissions, refuses to replace an earlier capture and removes a capture that failed:

```bash
(
  set -o pipefail -o noclobber
  umask 077
  helper="$bf_checkout/skills/bf-learn/scripts/evidence.py"
  policy='bf://example/concepts/archive-policy.md#retention'
  capture=actions/2026-09-25_retention-review/inputs/policy-v1.json
  mkdir -p "${capture%/*}"
  exec 3> "$capture" || exit 1
  bf read "$policy" | python3 "$helper" capture >&3 || { rm -f -- "$capture"; exit 1; }
  { cat "$capture"; bf read "$policy"; } | python3 "$helper" compare
)
```

The comparison returns `"state":"unchanged"`. Edit the policy body in this disposable copy and compare again:

```bash
sed -i.bak 's/latest version/two latest versions/' concepts/archive-policy.md
{
  cat actions/2026-09-25_retention-review/inputs/policy-v1.json
  bf read 'bf://example/concepts/archive-policy.md#retention'
} | python3 "$bf_checkout/skills/bf-learn/scripts/evidence.py" compare
```

It returns `"state":"changed"`, while the capture retains the old body. It does not revise the dependent decision for you. Incomplete evidence yields `unknown` or an error, never permission to dismiss an intention. The helper needs Python 3.11 or later. A direct pipe suits this short section: `bf read` returns the text of replies above 32 KiB in pages, which `capture` refuses. For those, pipe the helper's `read REF --brain BRAIN` mode instead: it runs `bf` from PATH and assembles the pages, as the [evidence guide](../../skills/bf-learn/references/evidence.md#retain-a-revision) shows.

### Share the procedure with a team brain

The action's `outputs/shared-procedure.md` and `outputs/share-manifest.json` are a prepared transfer candidate: one draft concept, no source-brain references and no asserted verification. It differs from this brain's own `concepts/selected-evidence.md`, which cites the private action that motivated it. Try it in a fresh disposable team brain:

```bash
bf_team="$bf_demo/team"
bf init "$bf_team" --name example-team
cp actions/2026-09-25_retention-review/outputs/shared-procedure.md "$bf_team/concepts/selected-evidence.md"
bf validate --brain "$bf_team"
bf read concepts/selected-evidence.md --brain "$bf_team"
```

The integration test exercises this isolated destination with retrieval cases. It checks this prepared fixture, not automatic redaction of arbitrary notes. For real transfers, use the [sharing guide](../../skills/bf-learn/references/share.md); review the complete candidate for its audience before an authorized write or publication.

## Clean up

Remove only this disposable copy and the shell wrapper:

```bash
cd "$bf_checkout"
unset -f bf
rm -rf -- "$bf_demo"
```
