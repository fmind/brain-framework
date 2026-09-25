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
bf register . --collect
bf update --dry-run
bf update
bf read
bf search retention
bf read demo:retention
bf read actions/2026-09-19_retention
bf validate
bf eval
```

`register --collect` trusts this copy to run `sensors/demo.py`; the wrapper keeps the demo's registry and state in its temporary directory, leaving your real registrations unchanged. A second update within the hour runs nothing. The fake sensor emits one fictional event inside each requested window, always with the same id, so repeated runs update one line in `memories/demo/`.

`bf read` shows the example project with its first open task, and `bf read actions/2026-09-19_retention` returns the action with its request file and the project that links to it. Continue that action by writing its answer with the record ref, then find it again:

```bash
cat > actions/2026-09-19_retention/outputs/answer.md <<'EOF'
# Answer

Keep originals because upstream content can disappear. Evidence: [record](demo:retention).
EOF
bf search "keep originals" --scope actions
bf validate
```

Then tick the action's tasks in `ACTION.md`, fill its Outcome and set `status: done`. For real data, create your own brain with `bf init PATH` and copy the patterns you need. See the [brain layout](../../docs/docs/brain.md).

## Follow an explicit link

```bash
bf read bf://example/concepts/retention.md
bf read bf://example/projects/example
bf read 'bf://example/projects/example.md#now'
```

The concept's `backlinks` list the project under `related-to`, and the project's `claims` list the same link: the project note owns a logical entity and its typed link preserves the exact origin section. `bf read repo:example/project` groups the collected record under its `repository` role. This example is fictional; do not copy its identities into a real brain.

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

These reads expose the evidence; an agent or person performs the review.

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
  test ! -e "$capture" || { echo "keep the existing capture: $capture" >&2; exit 1; }
  bf read "$policy" | python3 "$helper" capture > "$capture" || { rm -f -- "$capture"; exit 1; }
  { cat "$capture"; bf read "$policy"; } | python3 "$helper" compare
)
```

The comparison returns `"state":"unchanged"`. Edit the policy body in this disposable copy, for example `sed -i.bak 's/latest version/two latest versions/' concepts/archive-policy.md`, and compare again:

```bash
{
  cat actions/2026-09-25_retention-review/inputs/policy-v1.json
  bf read 'bf://example/concepts/archive-policy.md#retention'
} | python3 "$bf_checkout/skills/bf-learn/scripts/evidence.py" compare
```

It returns `"state":"changed"`, while the capture retains the old body. It does not revise the dependent decision for you. Incomplete evidence yields `unknown` or an error, never permission to dismiss an intention. The helper needs Python 3.11 or later.

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
