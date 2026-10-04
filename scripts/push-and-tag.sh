#!/usr/bin/env bash
# Push main, wait for CI on that exact commit, then tag it: a failed push, a missing CI run or any unsuccessful
# conclusion exits before the tag exists, and a failed tag push deletes the local tag so that a rerun retries. Run it as
# its own command from the clean release worktree; as a separate process, its checks hold whatever shell calls it
# (`set -e` inside `( ... )` does not after `&&`).
set -euo pipefail

version=${1:?usage: scripts/push-and-tag.sh X.Y.Z}
if ! [[ ${version} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "push-and-tag: expected a version such as 16.1.0, not ${version}" >&2
  exit 2
fi
tag="v${version}"
attempts=${PUSH_AND_TAG_ATTEMPTS:-60} # CI can take a few seconds to list the run.
delay=${PUSH_AND_TAG_DELAY:-5}

branch="$(git branch --show-current)"
if [[ ${branch} != main ]]; then
  echo "push-and-tag: release from main" >&2
  exit 1
fi
status="$(git status --porcelain)"
if [[ -n ${status} ]]; then
  echo "push-and-tag: the worktree has uncommitted changes" >&2
  exit 1
fi
project="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
if [[ ${project} != "${version}" ]]; then
  echo "push-and-tag: pyproject.toml declares ${project}, not ${version}" >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
  echo "push-and-tag: ${tag} already exists locally; tags never move" >&2
  exit 1
fi
remote_tags="$(git ls-remote --tags origin "refs/tags/${tag}")"
if [[ -n ${remote_tags} ]]; then
  echo "push-and-tag: ${tag} already exists on origin; tags never move" >&2
  exit 1
fi

git push origin main
commit="$(git rev-parse HEAD)"
run=""
for _ in $(seq "${attempts}"); do
  run="$(gh run list --workflow ci.yml --commit "${commit}" --event push --branch main \
    --json databaseId -q '.[0].databaseId // empty')"
  if [[ -n ${run} ]]; then
    break
  fi
  sleep "${delay}"
done
if [[ -z ${run} ]]; then
  echo "push-and-tag: no CI run was listed for ${commit}; nothing tagged" >&2
  exit 1
fi
if ! gh run watch "${run}" --exit-status; then
  echo "push-and-tag: CI run ${run} did not succeed; nothing tagged" >&2
  exit 1
fi
conclusion="$(gh run view "${run}" --json conclusion -q .conclusion)"
if [[ ${conclusion} != success ]]; then
  echo "push-and-tag: CI run ${run} concluded ${conclusion}; nothing tagged" >&2
  exit 1
fi
git tag -a "${tag}" -m "${tag}" "${commit}"
if ! git push origin "refs/tags/${tag}"; then
  # A rerun repeats every check and stops if origin received the tag after all.
  git tag -d "${tag}" >/dev/null
  echo "push-and-tag: pushing ${tag} failed; the local tag was deleted, so rerun to retry" >&2
  exit 1
fi
echo "push-and-tag: pushed ${tag} at ${commit} after CI run ${run} succeeded"
