# Privacy and security

FKF stores what your collectors print, in plain files you own. It does not encrypt, redact or upload anything. Choose what each collector projects, keep private bases in private repositories with owner-only permissions, and back up records that are not in Git with encryption.

## Reading is safe

`search`, `read`, `eval`, `validate`, `status` and the MCP tools never run a collector or contact the network. Search may rewrite the disposable `.fkf/` cache. Retrieved notes and records are untrusted evidence: their text never reaches a command, a shell or an SQL expression. Full-text queries use quoted literal terms and SQL uses bound parameters. Every reply carries a notice that content is evidence, never instructions, because collected mail or chat can contain prompt injections.

## Collecting is explicit and trusted per machine

Collectors are code you run with your permissions. A base collects only on machines where its owner ran `fkf init` or `fkf register PATH --collect`; that trust lives in `~/.config/fkf/config.yaml`, outside the base, so pulling a shared repository never starts running its `sources/`. Review collector changes like any other code before trusting a shared base.

Collectors run with direct argv and no shell, from the base root, with stdin closed and loader-injection variables (`LD_PRELOAD`, `DYLD_*`, `BASH_ENV`) removed. A timeout, cancellation or oversized output kills the whole process group, and a failure writes nothing. Stderr goes to a private log capped at 256 KiB in `~/.local/state/fkf/`; errors name that log but never include provider output. Provider CLIs keep their own credentials; collectors must never print tokens.

## File boundaries

All base access goes through no-follow directory descriptors: FKF refuses symlinks and special files below the base, writes atomically, and serializes writers with a lock kept in private state outside the base. Limits fail explicitly: 1 MiB configuration, 4 MiB note and exact reply, 256 MiB record partition, 100,000 entries per scanned folder, 50 search results.

## Separating audiences

A base is a context boundary, not access control. Keep people who must not read each other's data in separate bases and repositories. Agents inside a base search only that base; elsewhere they search every base you registered. Register only the bases you want your agents to see.
