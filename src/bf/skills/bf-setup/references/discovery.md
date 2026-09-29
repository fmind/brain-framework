# Discovery methods

Use only methods within the agreed scope. Never enumerate all browser profiles or walk the home directory to choose a scope on the user's behalf. Run bounded local processing before returning any private data to the agent.

## Tools and applications

Check a short list of tools relevant to the user's questions; the helper path is relative to this skill's folder:

```bash
python3 scripts/inventory.py tools git gh gws
```

The helper returns each name with `available: true|false`, using PATH lookup without execution; it omits executable paths and does not check credentials or versions. Names must be bare executable names, with at most 32 per invocation. It does not read stdin in this mode.

For graphical applications, inspect only names of immediate children in approved installation directories using local filesystem tools. Do not launch applications, inspect package contents or resolve links. Agree on an entry cap (for example 200) and report truncation. An unavailable path on Linux, macOS or a container is unknown coverage, not proof the host has no applications. Package-manager inventories are optional; use them only when their broader scope was approved.

## Bookmarks

Prefer a user-selected export: Netscape bookmark HTML or Chromium bookmark JSON with a `roots` object. For a live profile, agree on the exact bookmark file first; read a regular file without following symlinks and do not open history or account databases. Firefox database files are unsupported: request an export instead. Do not copy a whole profile. If a live file changes during inspection, report the observation as unstable or ask the user to provide an export.

After checking the approved file is regular, within the size limit and has no symlink components, run the helper locally. Replace the placeholder with the quoted approved path; never interpolate a discovered filename into shell code:

```bash
python3 scripts/inventory.py bookmarks --format html < "/approved/bookmarks.html"
python3 scripts/inventory.py bookmarks --format chromium < "/approved/Bookmarks"
```

For a synthetic export containing two links to `https://example.org/` with different paths, expect `{"hosts":[{"host":"example.org","count":2}],"skipped_urls":0}`; titles and URL paths must be absent.

It reads at most 4 MiB of UTF-8 input and accepts at most 20,000 bookmark/tree entries and 200 distinct hosts. Nothing reaches stdout on failure, and the fixed diagnostic says which applies. An exceeded limit names that limit: browsers export every bookmark at once, so export a single folder where the browser allows it, or delete folders in a copy of the export, and retry within approval. Duplicate JSON keys or malformed data report an unsupported export instead. HTTP(S) URLs contribute only a lowercase ASCII hostname and a count. Titles, folders, usernames, ports, paths, queries and fragments are omitted. Other schemes and unsupported or invalid URLs are counted under `skipped_urls`; hostnames remain sensitive. The helper never fetches URLs. Review the report locally first if the agent is not authorized to receive those hostnames.

Hostname counts show the selected export's contents, not usage frequency or an account inventory. Deduplicate integration recommendations across related hostnames only when evidence supports that they belong to the same service. Do not guess a registrable domain by taking the last two hostname labels.

## Project and document roots

Start with immediate directory names and file-type counts under explicitly approved roots, skipping hidden directories, dependency caches, symlinks and special files. Agree on depth and entry limits before traversal; report each reached limit. Recognizing a `.git` directory does not authorize reading remotes, commits or repository instructions. A second pass into selected manifests or documents requires authorization covering their contents; never dump all file contents to discover integrations.

Compare candidates against the selected brain's sensor configuration without running it. `bf update --dry-run --brain PATH` plans work without execution; `bf collect --dry-run` executes a sensor and is outside discovery. Existing configuration can contain private paths or account scopes: keep comparisons local and return only the minimum summary authorized for the agent.
