---
description: Install or update Brain Framework, match documentation to your version and verify the result.
---

# Install and update

## Install

Brain Framework supports Linux and macOS and requires Python 3.14 or newer. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then let it manage BF's isolated environment; `--python 3.14` selects the tested Python version, which uv downloads if needed:

```bash
uv tool install --python 3.14 brain-framework
bf --version
```

Without `--python`, uv may choose a newer Python for which some dependencies publish no prebuilt packages yet, and the installation can fail. uv records the choice, so `uv tool upgrade` keeps Python 3.14. If your shell cannot find `bf`, run `uv tool update-shell` and open a new shell. Follow [Getting started](getting-started.md) to create a brain and save your first decision.

## Match the docs to your version

This site follows the repository's current code, which can include changes newer than the package you installed. Use `bf --version` to identify your runtime and `bf COMMAND --help` for its accepted options. `bf schema` describes that installation's configuration format; newer schema kinds may require an update.

Copy example sensors, routines and demos from the release tag matching your runtime, `v$(bf --version)`, as the guides' commands do. Check [release notes](https://github.com/fmind/brain-framework/releases) and the [changelog](https://github.com/fmind/brain-framework/blob/main/CHANGELOG.md) before adopting a new feature. For a tagged release's source documentation, select the matching tag in the repository. Brain configuration and evaluation format versions are separate from the package version.

## Update

Stop active watchers or scheduled collection, read the release notes and back up the idle brain before updating. Major releases can require manual format changes described in the changelog.

```bash
uv tool upgrade brain-framework
cd ~/brain
bf --version
bf validate
bf eval
```

Expect `"valid":true` from validation and `"passed":true` from your retrieval suites. If either fails, fix the reported issue before resuming collection; see [Troubleshooting](troubleshooting.md).

`bf init` writes a brain's `AGENTS.md` once. Compare it with the installed release's template and merge new guidance by hand, as [Refresh brain instructions](agents.md#refresh-brain-instructions) shows.

Use the same release across a team's clones and collectors. Restart connected agent hosts and watchers after updating, then check collection health on the collecting machine. Regenerate native schedules when the release notes ask or the installed executable path moved. A successful package upgrade alone does not verify provider access or agent integration.
