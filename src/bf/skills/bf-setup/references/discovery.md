# Discovery methods

Use only methods within the agreed scope: never enumerate browser profiles or walk the home directory to choose a scope for the user, and process private data locally, within bounds, before returning anything.

## Tools and applications

Check a short list of tools relevant to the user's questions:

```bash
python3 "$SKILL_DIR/scripts/inventory.py" tools git gh gws
```

It returns each name with `available: true|false` from a PATH lookup, without running anything, printing paths or checking credentials; it takes at most 32 bare names.

For graphical applications, list only the names of immediate children of approved installation directories, within an agreed entry cap such as 200, never launching applications or resolving links. An unavailable path is unknown coverage, not proof that no applications exist.

## Bookmarks

Prefer a user-selected export: Netscape bookmark HTML or Chromium bookmark JSON. For a live profile, agree on the exact bookmark file, never open history or account databases and never copy the profile; Firefox databases are unsupported, so request an export. After checking that the approved file is regular, within the size limit and free of symlink components, run the helper on the quoted approved path, never interpolating a discovered filename into shell code:

```bash
python3 "$SKILL_DIR/scripts/inventory.py" bookmarks --format html < "/approved/bookmarks.html"
python3 "$SKILL_DIR/scripts/inventory.py" bookmarks --format chromium < "/approved/Bookmarks"
```

The helper reads at most 4 MiB, 20,000 entries and 200 distinct hosts and names an exceeded limit: export one folder, or delete folders in a copy, and retry within approval. Only lowercase hostnames and counts leave it, and they remain sensitive: review the report locally first if the agent may not receive them. Host counts describe the export, not usage or accounts.

## Project and document roots

Start with immediate directory names and file-type counts under explicitly approved roots, skipping hidden directories, dependency caches, symlinks and special files, within agreed depth and entry limits; report each reached limit. A `.git` directory does not authorize reading remotes, commits or repository instructions, and reading manifests or documents needs authorization covering their contents.

Compare candidates with the brain's configuration without running it: `bf update --dry-run --brain PATH` plans without executing. Configuration can hold private paths or account scopes: return only the authorized summary.
