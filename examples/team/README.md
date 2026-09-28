# Two contributors, one brain

Contributors share a brain through Git: separate records and action sessions merge cleanly, while competing revisions of one record conflict and fail validation until someone reconciles them. This walkthrough uses only `bf` and `git` on fictional evidence. It needs no provider or remote repository.

From the framework checkout after `uv sync --locked`, run this subshell. It isolates BF and Git configuration, gives the brain a two-line fixture sensor and removes everything on exit:

```bash
(
  set -eu
  bf_checkout=$PWD
  team_demo=$(mktemp -d)
  team_demo=$(cd "$team_demo" && pwd -P)
  trap 'rm -rf -- "$team_demo"' EXIT
  unset BF_BRAIN
  export XDG_CONFIG_HOME="$team_demo/config" XDG_STATE_HOME="$team_demo/state"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  bf() { uv run --project "$bf_checkout" bf "$@"; }
  bf init "$team_demo/brain" --name team
  cd "$team_demo/brain"
  git init --quiet --initial-branch=main
  git config user.name Example
  git config user.email example@example.invalid
  # Share the reviewed issues source through Git, as the team guide describes.
  printf '%s\n' /.bf/ '/memories/*' '!/memories/issues/' /originals/ /inputs/ > .gitignore
  mkdir sensors inputs
  printf '#!/bin/sh\nexec cat inputs/issues.json\n' > sensors/issues.sh
  chmod +x sensors/issues.sh
  printf 'sensors:\n  issues:\n    command: [sensors/issues.sh]\n' >> bf.yaml
  printf '[{"id": "shared", "title": "Original decision"}]\n' > inputs/issues.json
  bf collect issues
  git add --all
  git commit --quiet --message "Collect the original decision"

  # Two contributors add different records and independently named action sessions.
  for person in alice bob; do
    git switch --quiet --create "$person" main
    printf '[{"id": "%s", "title": "Independent evidence from %s"}]\n' "$person" "$person" > inputs/issues.json
    bf collect issues
    session="actions/2026-09-27_review-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    mkdir "$session"
    printf -- '---\ntype: action\nstatus: draft\n---\n\n# Review\n\n%s reviews the evidence.\n' "$person" > "$session/ACTION.md"
    git add --all
    git commit --quiet --message "Add evidence and a session from $person"
  done
  git merge --quiet --no-edit alice
  bf validate

  # Two more contributors revise the same record incompatibly.
  git switch --quiet --create combined
  for person in carol dana; do
    git switch --quiet --create "$person" combined
    printf '[{"id": "shared", "title": "Incompatible decision from %s"}]\n' "$person" > inputs/issues.json
    bf collect issues
    git commit --quiet --all --message "Revise the shared decision as $person"
  done
  git merge --no-edit carol || echo "The merge stopped on competing revisions."
  bf validate || echo "Validation rejected the unresolved record."
)
```

Each collection reports `"records":1`. After the independent merge, validation returns `{"notes":4,"problems":[],"records":3,"valid":true}`: the `shared`, `alice` and `bob` records, the two starter concepts and both action sessions. The same-record merge prints `CONFLICT (content): Merge conflict in memories/issues/SHA256.json`, where `SHA256` is the digest of the `shared` id. Validation then returns `"valid":false` with the problem `{"error":"invalid JSON document","file":"memories/issues/SHA256.json"}` and exits 1.

Each record has one stable source/ID-derived JSON filename, so different ids never collide. Actions use `YYYY-MM-DD_topic-UUID/ACTION.md`, with a fresh UUID for every independent session. Only competing revisions require judgment: preserve both sides, reconcile them from evidence, then validate. See the [team guide](../../docs/docs/team.md) and the [resolution procedure](../../skills/bf-maintain/references/conflicts.md).
