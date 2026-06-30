---
name: pr-loc-breakdown
description: Break down pure-Python lines-of-code changes per file for a PR (or two git refs), excluding non-.py files, blank lines, comments, and docstrings. Uses an AST/tokenize-based programmatic counter — never hand-counted. Input format: "<PR_NUMBER>" (e.g. "277") or "<BASE_REF> <HEAD_REF>".
argument-hint: "<PR_NUMBER> | <BASE_REF> <HEAD_REF>"
---

# PR Pure-Python LOC Breakdown

Produce an accurate per-file breakdown of **pure Python code** added/removed in a
PR. "Pure" means **only executable Python**: it excludes markdown/docs/other
non-`.py` files, blank lines, whole-line comments, inline comments, and
docstrings (module / class / function string-literal statements).

## Why a script (not eyeballing the diff)

`git diff --stat` counts every changed line including blanks, comments, and
docstrings, so it overstates code churn. Counting by reading the diff by hand is
error-prone and not reproducible. This skill ships `count_py_loc.py`, which:

1. Resolves the base & head SHAs (via `gh` for a PR number, or `git merge-base`
   for two refs — so churn that arrived from `main` moving forward is excluded).
2. Lists changed `.py` files with `git diff --name-only <base> <head> -- '*.py'`.
3. Reconstructs each file's **base** and **head** content (`git show <ref>:<path>`).
4. Strips comments + docstrings + blank lines from each version using Python's
   `ast` (docstring line ranges) and `tokenize` (whole-line comments).
5. Diffs the two cleaned versions with `difflib.unified_diff(n=0)` and counts
   `+`/`-` lines → real added/removed pure-code lines per file.

Because it diffs *cleaned* files, multi-line docstrings, reformatting, and
comment churn never inflate the numbers.

## Steps

1. Parse `$ARGUMENTS`:
   - One integer (e.g. `277` or `#277`) → PR mode (resolves refs via `gh`).
   - Two refs (e.g. `main feature-branch`) → ref mode.
   - Empty → defaults to `merge-base(main, HEAD)..HEAD`.
2. Run the bundled script from the repo root:
   ```bash
   python3 .claude/skills/pr-loc-breakdown/count_py_loc.py $ARGUMENTS
   ```
   (use `python3`; `python` may not be on PATH)
   Add `--json` for a machine-readable blob in addition to the table.
3. Present the table to the user. Lead with the total pure-Python added/removed,
   then the per-file rows (already sorted by churn, largest first).
4. If the user asked only for additions (or only a subset), filter the reported
   rows accordingly — do not re-count by hand.

## Notes

- Requires `gh` only in PR mode; ref mode and the default use `git` alone.
- The script self-heals on unparseable revisions: if a file version doesn't
  parse (partial/invalid Python at that SHA), it falls back to a blank+comment
  line filter and still strips full-line comments and blanks.
- Renames are counted as the diff between the old path's base content and the new
  path's head content as Git reports them; pure-rename-only files show 0/0 and
  are omitted.
- To reuse for non-PR comparisons (e.g. a tag range), pass two refs directly.

## Output Format

```
# Pure-Python LOC breakdown — PR #277
# base <sha>  ->  head <sha>

FILE                                    +add    -del     net
------------------------------------  ------  ------  ------
backend/app/services/foo.py              120      14    +106
mcp_server/server.py                      88      30     +58
...
------------------------------------  ------  ------  ------
TOTAL                                    512     110    +402

# N python file(s) with pure-code changes
```
