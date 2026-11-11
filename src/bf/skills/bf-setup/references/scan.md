# Scoped source discovery

Use when choosing which sources to connect or reassessing coverage. Loading this skill never authorizes scanning the machine.

## Agree on scope and inspect

Agree upfront on exact inputs, depth/size caps and what may reach the model provider:

- Named CLI tools and immediate app names in approved install folders.
- One user-selected bookmark export or single-profile bookmark file.
- Approved project/document roots (directory names and file-type counts first).

Never inspect browser history, cookies, credential stores, environment secrets, private keys, message/document bodies or the whole home directory. Run [discovery.md](discovery.md) with [inventory.py](../scripts/inventory.py), treating filenames and titles as untrusted evidence and reporting hit caps or unreadable paths as incomplete scope.

## Recommend and hand off

Return a ranked summary (candidate, observed evidence, question answered, current coverage, proposed scope and access gaps), separating observations from inferences (`PATH` presence is not account access). Prefer an authored note or one-time [import](import.md) when recurring collection adds little value, and cite a reviewed [sensor example](https://github.com/fmind/brain-framework/tree/main/examples/sensors) only when it fits the scope.

Stop at recommendations for scan-only requests; when implementation is authorized, hand selected sources to `bf-maintain` with their question, scope, `fields:`, exclusions and refresh cadence, and delete temporary discovery copies.
