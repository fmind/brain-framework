#!/usr/bin/env bash
# Verify a published release from the outside after CD succeeded: the remote tag's commit, a non-draft GitHub release
# holding exactly the wheel and source archive, PyPI digests matching them, their build attestations and a clean install
# from PyPI that initializes, validates, searches and reads a brain. It changes nothing but its own temporary folder.
set -euo pipefail

version=${1:?usage: scripts/verify-release.sh X.Y.Z}
if ! [[ ${version} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "verify-release: expected a version such as 16.1.0, not ${version}" >&2
  exit 2
fi
tag="v${version}"
repository=${VERIFY_RELEASE_REPOSITORY:-fmind/brain-framework}
attempts=${VERIFY_RELEASE_ATTEMPTS:-6} # PyPI's simple index can lag its JSON API for a minute after publication.
delay=${VERIFY_RELEASE_DELAY:-20}
wheel="brain_framework-${version}-py3-none-any.whl"
sdist="brain_framework-${version}.tar.gz"

fail() {
  echo "verify-release: $*" >&2
  exit 1
}

sha256() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | cut -d ' ' -f 1
  else
    shasum -a 256 "$1" | cut -d ' ' -f 1
  fi
}

# A disk-backed cache, not /tmp: the installed tool and its environment can be large for a RAM-backed /tmp.
cache="${XDG_CACHE_HOME:-${HOME}/.cache}"
mkdir -p "${cache}"
work="$(mktemp -d "${cache}/bf-verify-release.XXXXXX")"
trap 'rm -rf -- "${work}"' EXIT

commit="$(git ls-remote origin "refs/tags/${tag}^{}" | cut -f 1)" || fail "cannot list the tags of origin"
[[ -n ${commit} ]] || fail "no annotated ${tag} on origin"

release="$(gh release view "${tag}" --repo "${repository}" \
  --json isDraft,isPrerelease,url,assets --jq '[.isDraft, .isPrerelease, .url, ([.assets[].name] | sort | join(" "))] | @tsv')" ||
  fail "no GitHub release ${tag} in ${repository}"
IFS=$'\t' read -r draft prerelease url assets <<<"${release}"
[[ ${draft} == false ]] || fail "the GitHub release ${tag} is a draft"
[[ ${prerelease} == false ]] || fail "the GitHub release ${tag} is a prerelease"
[[ ${assets} == "${wheel} ${sdist}" ]] || fail "the GitHub release ${tag} holds '${assets}', not ${wheel} and ${sdist}"

gh release download "${tag}" --repo "${repository}" --dir "${work}/assets" ||
  fail "cannot download the assets of ${tag}"
json="$(curl -fsS --retry 3 "https://pypi.org/pypi/brain-framework/${version}/json")" ||
  fail "PyPI does not serve brain-framework ${version}"
pypi="$(printf '%s' "${json}" |
  python3 -c 'import json, sys; [print(u["filename"], u["digests"]["sha256"]) for u in json.load(sys.stdin)["urls"]]')" ||
  fail "PyPI's reply for ${version} lists no file digests"
count="$(printf '%s\n' "${pypi}" | grep -c . || true)"
[[ ${count} == 2 ]] || fail "PyPI lists ${count} files for ${version}, not the wheel and source archive"
for name in "${wheel}" "${sdist}"; do
  expected="$(printf '%s\n' "${pypi}" | awk -v name="${name}" '$1 == name { print $2 }')"
  [[ -n ${expected} ]] || fail "PyPI does not list ${name}"
  [[ "$(sha256 "${work}/assets/${name}")" == "${expected}" ]] || fail "${name} on GitHub does not match PyPI's SHA-256"
  gh attestation verify "${work}/assets/${name}" --repo "${repository}" >/dev/null ||
    fail "${name} has no valid build attestation from ${repository}"
done

# --no-config ignores user settings, such as an exclude-newer cutoff that refuses a minutes-old release.
export UV_TOOL_DIR="${work}/tools" UV_TOOL_BIN_DIR="${work}/bin"
installed=""
for _ in $(seq "${attempts}"); do
  if uv tool install --quiet --no-config --no-cache --python 3.14 "brain-framework==${version}"; then
    installed=yes
    break
  fi
  sleep "${delay}"
done
[[ -n ${installed} ]] || fail "could not install brain-framework==${version} from PyPI after ${attempts} attempts"

# The installed bf runs with its own configuration and state, never the caller's registered brains.
unset BF_BRAIN
export XDG_CONFIG_HOME="${work}/config" XDG_STATE_HOME="${work}/state"
bf="${work}/bin/bf"
brain="${work}/brain"
[[ "$("${bf}" --version)" == "${version}" ]] || fail "the installed bf does not report ${version}"
"${bf}" init "${brain}" >/dev/null
cat >"${brain}/projects/new-website.md" <<'EOF'
---
type: project
status: draft
summary: Launch a product website that helps visitors understand the product.
---

# New website

## Decision

Start with a single product page because visitors need a clear explanation before signing up.
EOF
"${bf}" validate --brain "${brain}" | grep -q '"valid":true' || fail "bf validate rejected the sample brain"
"${bf}" search "visitors clear explanation" --brain "${brain}" | grep -q '"ref":"projects/new-website.md#decision"' ||
  fail "bf search did not find the sample decision"
"${bf}" read "projects/new-website.md#decision" --brain "${brain}" | grep -q 'Start with a single product page' ||
  fail "bf read did not return the sample decision"

echo "verify-release: ${tag} at ${commit} is published at ${url}; PyPI digests and build attestations match ${wheel}" \
  "and ${sdist}, and the installed bf ${version} initialized, validated, searched and read a brain"
