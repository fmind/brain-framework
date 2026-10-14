# Write knowledge back

Use after a decision, a corrected assumption or completed work, when the user asks to remember it or the task authorizes it; for a review-only request, report the proposed edit instead. Keep the owning note short and current so a future session can act on it.

## Update the owning note

1. Find the owning note with `bf search "topic words"`: a project for state, decisions and next steps, a concept for reusable knowledge, an existing action for session detail. Read it whole and keep the reply's `sha256`; above 32 KiB, read the section you will change, whose `sha256` still names the whole file.
1. Make a focused edit that keeps unrelated paragraphs and stable anchors. Replace outdated current-state claims, but keep consequential earlier rationales with their evidence. Record a decision with its reason and supporting ref under `## Decision {#decision}`, and change `updated` only when the note's meaning changes.
1. Write only verified observations or what the user stated, marking proposals, unknowns and disputed evidence, and compare source revisions before replacing a claim.
1. Run `bf validate` and fix what your edit introduced, then search the question and read the returned ref. When the answer must stay findable, add a [retrieval case](https://fmind.github.io/brain-framework/docs/checks/) to an `evals/` suite and run `bf eval`. Report the changed refs, diff, checks and remaining uncertainty; commit only when authorized.

Never touch a file only to clear a review reminder. When two sessions changed the same claim, keep both and follow the `bf-maintain` skill's conflict guide.

## Guard a write

When your editor cannot detect another session's save, write through the guarded-write helper with the `sha256` of your read and a path relative to the working directory. Replace one passage, which must occur exactly once:

```bash
python3 "$SKILL_DIR/scripts/guarded-write.py" projects/new-website.md --expect-sha256 "$read_sha256" \
  --old "- [ ] Draft the product page." --new "- [x] Draft the product page."
```

Pass multi-line text with `--old-file` and `--new-file`; an empty `--new` deletes the passage. To replace the whole note, pipe its new text on stdin without `--old` and `--new`.

Expect `{"written":"projects/new-website.md","sha256":"..."}`: that digest guards your next write. Every refusal exits 1 and leaves the file untouched:

- `the file changed since it was read`: read it again, reapply your edit and retry with the new digest.
- `the old text does not occur` or `occurs several times`: copy the passage exactly, or include surrounding text until it is unique.
- `another guarded write of this file is still running`: read it again and retry.

Helpers lock the file while they check, write and rename it, so of two sessions that read the same digest only one succeeds; an editor that takes no lock can still save between the last check and the rename. The helper replaces only an existing regular file, atomically, following no symbolic link inside the brain; delete a `.write-HEX` file an interrupted edit left.

## Author notes

Start from [templates/project.md](../templates/project.md) or [templates/concept.md](../templates/concept.md), whose comments list the optional keys, and replace every sample value with an observed one. Keep work progress in the body: `status` is `draft`, `stable` or `deprecated`, and only `deprecated` closes a note and its tasks. Add `verified` events with real `by` and `at` values only after an actual check; a completed action alone justifies neither `status: stable` nor `verified`. Identities and tags follow [links](links.md).

Write next steps as checkboxes (`- [ ]`) in their owning note only: copied checkboxes become duplicate tasks.
