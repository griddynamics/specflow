# Spec: every register entry binds to exactly one query file

Status: specified, not implemented in `cypher_checks`. Implement it as a lint step when your
policy registers are long enough to go stale.

## What it enforces

The schema file's `policy` registers name queries by their file stem (see `RULES.md`, "Where
queries live"). `cypher-lint-owner-row-without-read` already holds `policy.read_owners` to the
read files. This check extends the same guarantee to every other register:

1. **No stale entry.** Each entry in `scan_shaped_queries`, `vector_read_queries`,
   `capped_traversals[].query` and the keys of `node_removals` binds to at least one `.cypher`
   file among the checked targets. A stale entry is an allowance waiting for a new file to
   inherit it by accident.
2. **No duplicated statement.** No entry binds to two files. Two files with the same normalised
   stem in different folders are one statement held twice; they resolve the same cap, key and
   exemption, and disagree as soon as one is fixed.

DDL files (`policy.ddl_globs`) are outside both rules: they are not register entries.

## Input

- The schema file the other checks read (`--schema`), parsed by `cypher_checks.schema`.
- The complete set of `.cypher` targets. The check is only meaningful over every query file at
  once, so run it from the `all` entry point or with every query directory as a target.
- Optional: an allow-list of entries whose files live outside the targets (for example query
  files that only test fixtures use), in a new `policy.register_exempt` list of names.

## Algorithm

```text
targets  = expand_targets(paths) minus files matching policy.ddl_globs
by_stem  = group targets by normalize_query_name(path.stem)
for (register, name) in all register entries:
    if name in policy.register_exempt: continue
    files = by_stem.get(normalize_query_name(name), [])
    if not files:      finding "cypher-lint-register-entry-without-file"   at "policy.<register>"
    if len(files) > 1: finding "cypher-lint-duplicate-query-stem"         at each file, line 1
```

Report paths relative to `--root`, cite `RULES.md#one-statement-per-file`, and exit with the
shared contract (0 clean, 1 findings, 2 configuration error).

## Example

With the example schema and the example queries:

```yaml
policy:
  scan_shaped_queries: ["All genres", "All movies"]
```

`All genres` binds to `examples/queries/read/all_genres.cypher`. `All movies` binds to no file
under `examples/queries` (only a test fixture uses it), so the check reports
`cypher-lint-register-entry-without-file` at `policy.scan_shaped_queries` unless
`policy.register_exempt: ["All movies"]` is declared. Adding a second
`people/queries/read/all_genres.cypher` reports `cypher-lint-duplicate-query-stem` on both files.
