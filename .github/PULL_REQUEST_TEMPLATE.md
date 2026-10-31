## What

<!-- Describe the problem and resulting behavior. For sensor changes, name the provider CLI and version verified. -->

## Why

<!-- Explain the user or maintenance benefit, and the trade-offs worth knowing about. -->

## Test plan

- [ ] `mise run all` is green
- [ ] User-facing docs, the owning skill and a runnable example with its expected result are updated
- [ ] `CHANGELOG.md` records user-visible changes under `Unreleased`
- [ ] No real brain, workspace data, or personal identifier appears in the diff
- [ ] Any break to a command, flag, configuration key, environment variable, stored format, reference format, reply field or schema, or MCP tool name is stated above and deferred to the next major window ([keep the contract](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md#keep-the-contract)), unless this PR prepares that major release with manual upgrade steps in `CHANGELOG.md`; there is no compatibility layer
