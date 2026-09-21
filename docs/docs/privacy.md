# Privacy and security

FKF reads local evidence and executes configured source commands only through explicit collection. It does not read provider credential files, redact source output, encrypt the base, or sandbox adapters. Review the selected fields and the commands that produce them.

Stored reads and MCP never fetch data or execute commands. Retrieved records and notes are untrusted evidence. Their text is never inserted into a command, executable position or SQL expression. FTS queries use quoted literal terms; SQL uses bound parameters.

The owner controls source configuration and adapter code. Collection runs their current contents without a separate approval step. Keep credentials out of adapters and configuration. Runtime startup/loader variables and unsafe PATH/home/config roots are removed from children. Cancellation and timeouts terminate POSIX process groups.

The Store admits regular files beneath published projects/, wiki/, tasks/ and records/ through no-follow directory descriptors. It refuses descendant symlinks and special files, bounds directory traversal, and serializes mutations by physical base identity. Lock files live in private per-base state outside the base.

The principal limits are: 1 MiB configuration/source helper file; 4 MiB note or exact reply; 16 MiB capture; 20,000 entries per traversed tree; 100,000 captured records; 512 MiB searchable corpus/index; 100 returned search hits. Exceeding a bound fails explicitly.

Retrieval opens the published index file read-only in place after checking that its manifest still matches the current notes, records and configuration; a missing, stale or damaged index stops indexed retrieval with an explicit build instruction. Back up durable notes, records, configuration and adapters. SQLite is disposable. Keep private bases and recovery archives under appropriate owner-only permissions and encryption. Rehearse restore, rebuild and exact evidence reads; Git alone is insufficient when collected evidence is ignored.

The v7 source tree passing local tests does not establish installed-runtime, provider-freshness, hosted-release or off-device recovery success.
