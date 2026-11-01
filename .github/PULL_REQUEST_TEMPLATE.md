## What

<!-- Describe the problem and resulting behavior. For sensor changes, name the provider CLI and version verified. -->

## Why

<!-- Explain the user or maintenance benefit, and the trade-offs worth knowing about. -->

## Test plan

- [ ] `mise run all` is green
- [ ] User-facing docs, the owning skill and a runnable example with its expected result are updated
- [ ] `CHANGELOG.md` records user-visible changes under `Unreleased`
- [ ] No real brain, workspace data, or personal identifier appears in the diff
- [ ] Any break to a command, flag, configuration key, environment variable, stored format, reference format, reply field or schema, or MCP tool name is stated above and recorded under `### Breaking changes` in `CHANGELOG.md` with its manual upgrade steps, for the next major release ([keep the contract](https://github.com/fmind/brain-framework/blob/main/CONTRIBUTING.md#keep-the-contract)); there is no compatibility layer
