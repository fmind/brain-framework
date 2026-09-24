# Runnable example brain

A fictional project, OKF concept, resumable action and local sensor demonstrate the complete workflow without credentials or network access. Run this from the Brain Framework checkout after `uv sync --locked`:

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
bf search retention
bf read demo:retention
bf validate
bf eval
```

`register --collect` trusts this copy to run `sensors/demo.py`; the wrapper keeps the demo's registry and state in its temporary directory, leaving your real registrations unchanged. A second update within the hour runs nothing. The fake sensor emits one fictional event inside each requested window, always with the same id, so repeated runs update one line in `memories/demo/`.

Continue the action under `actions/2026-09-19_retention/`: write the answer with the record ref `demo:retention`, update ACTION.md, then validate and evaluate. For real data, create your own brain with `bf init PATH` and copy the patterns you need. See the [brain layout](../../docs/docs/brain.md).

When finished, remove only this disposable copy and the shell wrapper:

```bash
cd "$bf_checkout"
unset -f bf
rm -rf -- "$bf_demo"
```
