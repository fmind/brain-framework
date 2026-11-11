# Discovery methods

Stay strictly inside agreed inputs and bounds, summarizing locally before sending results to the model.

## Tools, bookmarks and roots

```bash
python3 SKILL_DIR/scripts/inventory.py tools git gh gws
python3 SKILL_DIR/scripts/inventory.py bookmarks --format html < "/approved/bookmarks.html"
python3 SKILL_DIR/scripts/inventory.py bookmarks --format chromium < "/approved/Bookmarks"
```

- **Tools**: checks up to 32 bare names on `PATH` (`available: true|false`) without executing them or printing paths. List GUI app names only as immediate children of approved directories (capped, e.g., 200 entries) without launching them.
- **Bookmarks**: verify the approved file is regular, symlink-free and bounded (helper caps: 4 MiB, 20,000 entries, 200 lowercase hostnames; Firefox databases require an HTML export). Never open history or account databases.
- **Project/document roots**: count immediate directories and file extensions under approved roots, skipping hidden folders, dependency caches, symlinks and special files; `.git` presence never authorizes reading commits or remotes. Compare against `bf update --dry-run --brain PATH` without executing programs.
