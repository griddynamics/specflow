# Cypher rules

How to write Cypher that the checks in this kit accept, and why each rule exists. Every finding
the kit prints ends in `doc: RULES.md#<heading>`, which points to a section of this file.

Graph queries are the part of a system most likely to be quietly wrong. A wrong Cypher query
rarely fails. It returns a plausible, incomplete answer, so reviewing the query text and reading
the query plan are both required.

The examples use the kit's example graph (`examples/schema.yaml`). It has `Person`, `Movie` and
`Genre` nodes, all carrying the common label `Entity`, linked by `ACTED_IN`, `DIRECTED`,
`IN_GENRE` and `FOLLOWS` relationships.

## Modelling for queries

The shape of the graph decides which queries are possible and which are fast. These points look
like modelling, but they are query concerns.

- **Use a typed relationship, never one generic type told apart by a property.** With
  `-[:ACTED_IN]->`, the engine expands only those relationships. With a generic
  `-[:LINK {kind: 'ACTED_IN'}]->`, it expands every `LINK` on the node and then tests each one.
  The relationship type is the cheapest filter in the language because it is the only one
  applied *before* the traversal. An index on the discriminator property does not help: it finds
  the edges but does not stop the expansion. If the model is imported from another system and
  already uses a generic type, index the discriminator and accept that the filter runs after the
  expansion. Never add a generic type to a graph you own.
- **Relationship properties qualify an edge; they do not identify it.** Weight, order,
  confidence and provenance belong on the edge. The kind of connection belongs in the type.
- **Direction is part of the model.** Store each fact in one direction and traverse it the other
  way when a read needs that. Never write the same fact as two opposite edges: that doubles the
  write and lets the two edges disagree.
- **Put the properties you filter on the node, and index them.** A property that is only
  returned needs no index. A property used in a `WHERE` on a large label does.
- **A relationship type is a distinction that reads make.** If a read must tell apart two
  connections between the same pair of labels, they are two types, not one type with a property.

## Where queries live

Each query is one statement in its own `.cypher` file. Read queries go in a read folder, write
queries in a write folder, and schema DDL in a schema folder. The kit's `policy.write_globs` and
`policy.ddl_globs` describe this layout to the checks. Query text is never written inline in
application code and never assembled at runtime.

Comments in Cypher use `//` or `/* ... */`, never SQL-style `--`. In Cypher, `--` is part of a
relationship arrow (`-->`), and an SQL-style comment fails validation.

**A file's stem is its name in every policy register.** The registers in the schema file's
`policy` section name queries, for example `capped_traversals: [{query: "Follower network"}]`.
A register entry binds to the file whose stem matches it after normalisation: case is ignored,
and `_`, `-` and spaces are the same. So `follower_network.cypher` binds to `Follower network`.
Nothing else declares the binding, so nothing else can drift from it. A rename of the query is a
rename of the file, of the register entry and of the constant that loads it, all in one change.

### One statement per file

Each file holds exactly one statement, and each statement is in exactly one file. If two
modules need the same read, they both load the single file; they never keep copies. Two copies
of one statement bind the same register entries twice and disagree as soon as one is fixed.

DDL files (`policy.ddl_globs`) are the exception: they hold several statements separated by `;`,
and the checks split them because Neo4j takes one DDL statement per call.

Every file starts with a header comment that states the query's purpose. For a bounded path, the
header also names the configuration key of its cap.

| Rule | Finding |
|---|---|
| `cypher-lint-missing-header-comment` | The file does not start with a `//` or `/* */` comment. |
| `cypher-lint-multiple-statements` | A query file (not DDL) holds more than one statement. |

### No inline queries

Query text is never a string in application code. A query lives in a `.cypher` (or `.sql`)
file and the code loads it once into a constant through a loader function, then passes that
constant to the driver unchanged. The `no-inline-queries` command scans a Python tree:

| Rule | Finding |
|---|---|
| `no-inline-queries-cypher` | A string constant (or f-string) that looks like Cypher. |
| `no-inline-queries-sql` | A string constant that looks like SQL. |
| `no-inline-queries-missing-file` | A loader call whose path argument resolves to no file. |
| `no-inline-queries-orphan-file` | A query file under a `--query-dir` that no loader call loads. |

Docstrings are exempt. Test code and fixtures are skipped by default. In a migration module,
only raw text passed to `op.execute(...)` or `sa.text(...)` counts. Loaders are declared as
`--loader NAME=BASE_DIR`, so any loader function name and folder layout works.

The semgrep rule `neo4j.loaded-query-mutated` covers the other half: a loaded query that is
concatenated, `%`-formatted, `.format`ted, `.replace`d or put in an f-string before it runs.

### Parameter names match call sites

A loaded query reaches the driver as `tx.run(CONSTANT, key=value, ...)`. Every keyword passed
there must be a `$key` the query file declares. A keyword the file does not declare is a typo
or a stale call, and the parameter the query expects arrives as `null`.

| Rule | Finding |
|---|---|
| `cypher-lint-param-mismatch` | A `.run(CONSTANT, ...)` call passes a keyword the loaded file does not declare as `$keyword`. |

The `param-names` command finds every `CONSTANT = LOADER("path")` assignment for the loaders you
name, then checks every `<receiver>.run(CONSTANT, ...)` call in the tree. A `$name` that appears
only in a comment of the query file does not count as declared.

### Every read has an owner

Optional. When `policy.read_globs` is set, every read file must be named in
`policy.read_owners`, and every entry there must name a read file among the checked targets.
The register is then the reviewed list of the reads the system has, and a read nobody owns
cannot be added silently.

| Rule | Finding |
|---|---|
| `cypher-lint-read-without-owner` | A read file whose stem names no `policy.read_owners` entry. |
| `cypher-lint-owner-row-without-read` | A `policy.read_owners` entry that names no read file among the targets. |

Run lint over every read file at once, because the second rule needs the full set.

## Rules

### Parameterize everything

Never use string interpolation, concatenation or runtime-assembled query text. Every value is a
`$parameter`.

This is two rules in one. It keeps queries safe from injection, and it lets the database reuse a
plan it already built. An interpolated query is a new query string every time, so the plan cache
never hits.

Structure cannot be parameterized: a label, a relationship type or a path quantifier is part of
the query text, not a value. When structure must vary, write each variant as its own query file
and name the file after what makes it differ.

| Rule | Finding |
|---|---|
| `cypher-lint-templating-marker` | `{{`, `%s`, `apoc.cypher.run`, or a dynamic label such as `(n:$label)`. |

### Anchor first, expand second

Every query starts from a small set of nodes found through an index, then grows outward. Never
start with a scan of a whole label. The anchor makes the traversal proportional to the
neighbourhood rather than to the database.

**An anchor is a label that carries a uniqueness constraint or range index, plus that indexed
property, in the same pattern.** Both halves are needed. An index belongs to one label, so a
pattern without that label, or with a different label, cannot use it:

```cypher
MATCH (n:Entity {id: $id})              // unique index seek (the constraint is on Entity.id)
MATCH (p:Person:Entity {id: $id})       // seek, then the second label filters one row
MATCH (p:Person {id: $id})              // plan check: NodeByLabelScan when Person is in policy.banned_scan_labels
MATCH (n {id: $id})                     // plan check: AllNodesScan
```

The static rule is looser than the plan check. It accepts any inline key on a labelled node, and
any anchorable inline key on an unlabelled node, so the two shapes above pass `lint` and are
caught by the live `explain` step, which reads the plan the database chose.

A second label costs nothing and narrows what the anchor can be. Name it whenever the query
depends on the node's kind, because a shared id space does not stop a traversal from starting at
a node of the wrong kind.

The anchorable properties are the schema file's `constraints.unique` and `constraints.indexes`.
A uniqueness constraint and a range index anchor alike when the property is compared to a
`$parameter` with `=` or `IN`, either in the pattern or in the `WHERE` right after it. A
comparison that cannot seek (`>`, `<`, `STARTS WITH`, `CONTAINS`, a regular expression) narrows
the result after the anchor but is never the anchor. A literal the caller does not supply is not
an anchor either.

If a query needs an anchor the graph does not index, **add the index** to the schema file and
the DDL. Do not add the query to the exemption register: an exemption granted for a missing
index makes the scan permanent.

`policy.scan_shaped_queries` is for reads whose shape leaves nothing to anchor on. Examples are a
read whose result is every row of a label, a read whose candidates come from a vector or
full-text index procedure, and a read over a disjunction that no single index covers. A scan
anywhere else is a defect, and so is a register entry whose scan an index would remove.

| Rule | Finding |
|---|---|
| `cypher-lint-not-anchored` | The first `MATCH`/`MERGE` of a query part has neither a labelled node with an inline key or a `WHERE` `=`/`IN` seek on its variable, nor an unlabelled node seeking an anchorable property by `$parameter`, and the file is not in `policy.scan_shaped_queries`. Unindexed or literal keys on a labelled node are left to the `explain` plan check. |

### Bound every variable-length path

```cypher
-[:FOLLOWS*]->                                                  // finding: no bound
-[:FOLLOWS*1..4]->                                              // finding: no configured bound
MATCH p = (a:Person:Entity {id: $id})-[:FOLLOWS*1..4]->(b:Person)
WHERE length(p) <= $follow_max_depth                            // correct
```

Cypher accepts no parameter inside a variable-length bound, so a bounded walk always has two
parts:

- a **literal hard cap** in the pattern, the largest value the configuration key may ever take;
- a `length(p) <= $<key>` predicate that carries the **effective bound** from configuration.

An unbounded walk over a large graph is an outage. A literal bound with no configuration
predicate is just as rigid and hides the fact that an operator could lower it without a code
change. Raising the hard cap is a reviewed code change. Lowering the effective depth is a
configuration change alone.

Each such query is registered in `policy.capped_traversals` with its hard cap and the name of
its configuration key. The key is compared without regard to case, so `FOLLOW_MAX_DEPTH`
matches `$follow_max_depth`.

| Rule | Finding |
|---|---|
| `cypher-lint-unbounded-path` | A `*` or quantified path pattern without an upper bound. |
| `cypher-lint-missing-length-filter` | A bounded variable-length path without a `length(p) <= $key` filter. |
| `cypher-lint-hard-cap-exceeded` | The literal upper bound is larger than the registered hard cap. |
| `cypher-lint-wrong-config-key` | The `length()` filter uses a parameter other than the registered key. |
| `cypher-lint-unregistered-capped-query` | A file with a variable-length path whose stem names no `policy.capped_traversals` entry, so its cap is never checked. |
| `cap-mismatch` (explain) | The plan does not expand to exactly the registered cap. |

### Prune inside the pattern, not after

A predicate inside the pattern is applied at each hop, so the traversal never expands what it
would later throw away. The same predicate in a later `WHERE`, or in application code, collects
first and filters second.

```cypher
MATCH p = (a:Person:Entity {id: $id}) ( ()-[:FOLLOWS]->(x:Person WHERE x.born >= $born_after) ){1,3} (b:Person)
```

The difference does not show on a small graph and dominates on a large one. Let the store do the
data work: filtering, ordering, cutting to a count and scoring all happen in the query, so the
caller never receives rows only to discard, sort or cut them. Two reads of one store that the
caller would intersect, merge or re-rank should be one query.

### Deterministic cuts

A `LIMIT` cuts a result at a position, so the order must be fixed and total. Otherwise the same
query returns different rows from run to run.

The `ORDER BY` before a `LIMIT` is total when its last key is unique: a uniquely constrained
property of a node the query binds, its `elementId`, or an alias of either. Ordering by every
column the projection returns is also total. A parameter's field, or an alias of anything else,
is not a unique key.

```cypher
RETURN m.id AS id, m.title AS title, score
ORDER BY score DESC, id            // total: id is unique
LIMIT $limit
```

| Rule | Finding |
|---|---|
| `cypher-lint-limit-without-order` | A `LIMIT` whose projection has no `ORDER BY`. |
| `cypher-lint-order-not-total` | An `ORDER BY` before a `LIMIT` that does not end in a unique key (and does not cover every column). |

### Keep traversals short and typed

Name every relationship type you expand; an untyped hop expands everything. Prefer several
short, typed traversals staged with `WITH` over one long pattern the planner must satisfy in one
step.

### Schema vocabulary

A query uses only the vocabulary the schema file declares. A misspelled relationship type or
property is not an error to the database: it simply matches nothing, and the query returns a
plausible empty answer.

| Rule | Finding |
|---|---|
| `cypher-lint-unknown-relationship-type` | A relationship type (including each member of an `A\|B` alternation) that the schema's `relationships` does not declare. |
| `cypher-lint-set-unknown-property` | A `SET`, `MERGE` or `CREATE` map that writes a property the label or type does not declare. |

The live checks ([live schema validation](#live-schema-validation)) also catch unknown labels,
properties and wrong directions against the seeded database.

### Write only where allowed

Writes happen only in the files meant for them. Everything else is a read.

- `policy.write_globs` names the write files. A write clause (`CREATE`, `MERGE`, `SET`,
  `DELETE`, `REMOVE`, `FOREACH`) in any other file is a finding. An empty list turns the rule
  off.
- `policy.write_allowed_globs` (optional) names the locations a write file may live in, for
  example only the write folders of the modules allowed to write.

In application code, reads run in read transactions and writes in write transactions, both
managed by the driver. The semgrep rules `neo4j.autocommit-session-run`,
`neo4j.driver-constructed-outside-graph-module` and `neo4j.ad-hoc-session-outside-graph-module`
keep the driver and its sessions in one graph module.

| Rule | Finding |
|---|---|
| `cypher-lint-write-outside-write-folder` | A write clause in a file `policy.write_globs` does not match. |
| `cypher-lint-write-outside-capability` | A write file outside `policy.write_allowed_globs`. |

### `MERGE` needs a full key

A `MERGE` on a partial pattern creates duplicates under concurrency. A node `MERGE` maps exactly
one uniquely constrained property to a `$parameter`, then `SET`s the rest. A bounded batch may
instead use one property of the row bound by an immediately preceding `UNWIND $rows AS row`. No
expression or intermediate rebinding may invent the key.

```cypher
MERGE (p:Person:Entity {id: $id})
SET p.name = $name, p.born = $born
```

Two schema allowances widen this, both empty by default. When every uniqueness constraint is
composite with a tenant key, `policy.merge_scope_keys` names it: the `MERGE` map carries it beside
the one key, and it too must be a `$parameter`. A batch write named in
`policy.batch_merge_queries` may key on a row bound by an earlier `UNWIND` that is not the first
clause (a `MATCH` of the parent comes first), or by an `UNWIND` of a list property of such a row;
the row variable is still never rebound.

The pattern names the label that holds the constraint, for the same reason an anchor does: a
constraint belongs to one label, and a `MERGE` that misses it takes no lock and duplicates.

A `MERGE` on a pattern of several nodes and relationships merges the *whole pattern*: if any
part is missing, it creates all of it. Merge the nodes first, then the relationship between them.
A relationship `MERGE` matches only between endpoints an earlier clause already bound.

A relationship `MERGE` key holds only what identifies the edge. A property the write
accumulates, such as a list of sources, is `SET` after the match and never placed in the
pattern. Inside the key, the second source matches nothing and creates a parallel edge.

| Rule | Finding |
|---|---|
| `cypher-lint-merge-no-key` | A node `MERGE` with no property map. |
| `cypher-lint-merge-multiple-keys` | A node `MERGE` whose map holds more than one property. |
| `cypher-lint-merge-key-not-parameter` | The key is not a `$parameter` (or an `UNWIND` row field). |
| `cypher-lint-merge-key-label-unconstrained` | The single key is not uniquely constrained (`constraints.unique`) on any label the pattern names, so the `MERGE` takes no lock. Off when the schema declares no unique constraint. |

### No data `CREATE`, no node `DELETE`

Every data write is a `MERGE`, which is what makes re-running a write converge instead of
duplicating. `CREATE` is for DDL only, with one allowance below. Nodes are not deleted, except by
the files `policy.node_removals` registers, and each of those only for the labels and
relationship types it lists. Deleting a relationship is always allowed.

**One allowance: re-pointing an edge.** When two duplicate nodes are merged, an edge may need to
move to the surviving node and keep its identity: two edges of one type between one pair of nodes
stay two. Neo4j has no operation that moves a relationship endpoint, and a `MERGE` onto the
survivor would fold parallel edges into one. A re-point therefore matches the edge by
`elementId` (or matches every edge of the merged node by a relationship pattern),
`CREATE`s its copy on the survivor with the properties it read, and `DELETE`s the original by
that variable, in one statement. Only the files `policy.create_allowed_globs` matches may do this.

```cypher
MATCH (old:Person:Entity {id: $old_id})-[r:ACTED_IN]->(m:Movie)
WHERE elementId(r) = $rel_id
MATCH (survivor:Person:Entity {id: $survivor_id})
CREATE (survivor)-[moved:ACTED_IN]->(m)
SET moved = properties(r)
DELETE r
```

| Rule | Finding |
|---|---|
| `cypher-lint-create-not-allowed` | A data `CREATE` outside the DDL files and the re-point files. |
| `cypher-lint-delete-not-allowed` | A node `DELETE` outside `policy.node_removals`, or of a label the entry does not list. |

### Set shared properties once

`ON CREATE SET` and `ON MATCH SET` carry only what differs between the two branches: a
create-only default or timestamp, or a match-only `coalesce` that keeps the stored value. A
property assigned the same value in both branches is a plain `SET` after the `MERGE`, written
once. Two copies of one property list drift as soon as a property is added to only one of them.

```cypher
MERGE (p:Person:Entity {id: $id})
ON CREATE SET p.status = 'ACTIVE'
ON MATCH SET  p.status = coalesce(p.status, 'ACTIVE')
SET p.name = $name, p.born = $born
```

| Rule | Finding |
|---|---|
| `cypher-lint-shared-set-in-both-branches` | The same property set to the same value in both `ON CREATE SET` and `ON MATCH SET`. |

### One statement, one concern

A query that does two jobs is two queries. A single statement that does two jobs cannot be
named, reviewed, retried or explained as one thing.

### Return only what the caller uses

Returning a node ships every property on it. Name the properties the caller reads. This is a
payload rule and a coupling rule: a caller that receives whole nodes comes to depend on
properties the query never promised. If a node must be returned whole, it feeds exactly one
projection in the code, and that projection decides what leaves the module.

**Vectors stay in the graph.** An embedding property (a property typed `vector` in the schema
file) is compared where it is stored: a vector-index query, or `vector.similarity.cosine` inside
the query that needs the similarity, returns the score and never the vector. A read returns a
vector only when `policy.vector_read_queries` names it. Every other read leaves vectors out. A
map projection cannot omit a key, so a `.*` projection nulls each vector property of its label:
`m {.*, plot_embedding: null}`.

| Rule | Finding |
|---|---|
| `cypher-lint-unregistered-vector-read` | A `RETURN` that projects a vector property, by property access, by a `.*` projection that does not null every vector property, or as a whole node of a label that has one, in a file `policy.vector_read_queries` does not name. A node not pinned to one label is taken to carry every vector. |

## Reading the plan

A performance claim about a Cypher query that is not backed by a plan is a guess.

- `EXPLAIN` shows the plan the planner chose. `PROFILE` runs the query and reports what it
  touched: rows and database hits per operator.
- Read the row counts between operators. The step where the number explodes is the problem, and
  it is usually an expand before a filter that could have come first.
- A full scan of a label the query traverses means the anchor or the index is missing.
- Two `MATCH` clauses with no variable in common produce a cartesian product. The plan names it;
  the result set hides it.

The `explain` command runs `EXPLAIN` on every statement against a throwaway Neo4j seeded with a
fixture graph built from the schema file. Parameters get dummy values; when the server reports a
type mismatch, the parameter is retyped from the type the server names and planned again.

| Rule | Finding |
|---|---|
| a GQLSTATUS code, e.g. `01N01`, `03N90` | Any server notification: a deprecation, a cartesian product, an unknown label, an exhaustive shortest path and so on. |
| `AllNodesScan` | A scan of every node, outside `policy.scan_shaped_queries`. |
| `NodeByLabelScan` | A label scan (including the multi-label intersection and union variants) of a `policy.banned_scan_labels` label, outside `policy.scan_shaped_queries`. |
| `cap-mismatch` | A registered capped walk not planned to its registered cap. |
| a server error code, or `cypher-explain-error` | The statement could not be planned. |

## Live schema validation

The `cyver` command runs the CyVer validators against the same seeded throwaway database:

- **syntax**: the statement parses;
- **schema**: every label, relationship type and path direction exists in the database;
- **properties**: every property a pattern reads or writes exists on its label or type.

| Rule | Finding |
|---|---|
| `cyver-schema-score`, `cyver-properties-score` | A validator score below 1.0. |
| a CyVer metadata code | The problem the validator found, with its description. |

The database is seeded from the schema file, so the schema file must describe the real graph.
`derive-schema` drafts it from a development database, and [schema parity](#schema-parity) holds
it to the DDL.

## Schema parity

The schema file and the DDL files must declare the same uniqueness constraints and range
indexes, and every DDL `CREATE` must be idempotent so it can run on every start.

| Rule | Finding |
|---|---|
| `parity-unique-missing-in-ddl` | A `constraints.unique` entry no DDL statement creates. |
| `parity-unique-undeclared` | A uniqueness constraint the DDL creates but the schema file does not declare. |
| `parity-index-missing-in-ddl` | A `constraints.indexes` entry no DDL statement creates. |
| `parity-index-undeclared` | A range index the DDL creates but the schema file does not declare. |
| `parity-ddl-not-idempotent` | A `CREATE` without `IF NOT EXISTS`. |
| `parity-no-ddl-files` | No DDL files to compare: a finding, never a skip. |

Vector and full-text indexes, and composite indexes, are outside this comparison.

A placeholder label (a token a query or DDL template stands for, such as `__FOREIGN_LABEL__`) is
named in `policy.placeholder_labels` with the concrete labels it stands for in the project. A
declared `Placeholder.property` entry then asks for the DDL of `Concrete.property` for every
concrete label: an empty list asks for none, so the entry is satisfied, and a non-empty list makes
each missing concrete statement a `parity-*-missing-in-ddl` finding. A DDL template that targets the
placeholder itself creates the declared entry and is not undeclared. A placeholder outside the
register is compared like any other label, so its entry needs a DDL statement on the placeholder.

## Correctness

Cypher fails with a quiet wrong answer, so these traps deserve as much attention as performance.

- **`null` does not compare.** A comparison against a missing property is neither true nor
  false, so a `WHERE` on an optional property silently drops rows. Test for `IS NULL`
  explicitly when absence is meaningful.
- **`OPTIONAL MATCH` means "keep the row, with nulls".** It never removes a row, and a filter
  written after it on its variables turns it back into a required match. A misplaced
  `OPTIONAL MATCH` multiplies rows.
- **Decide whether you want `DISTINCT`, and why.** A path pattern can reach the same node by
  more than one route; aggregating without deciding this counts some things twice.
- **Aggregate in stages.** `WITH` closes one scope and opens the next: aggregate, filter the
  aggregate, then expand again. Anything a `WITH` does not carry forward is gone.
- **The dangerous test is the one that returns too few rows.** An empty result looks like "no
  results" rather than a bug. Cover the empty case, the single case and the many case, and assert
  the count, not merely that something came back.

## Formatting

- Clause keywords are uppercase and start a line.
- One `MATCH` or `OPTIONAL MATCH` per line.
- Bind only what you return.
- Put a comment above any non-obvious pattern explaining what it is for, especially quantified
  path patterns, which few readers have seen.

| Rule | Finding |
|---|---|
| `cypher-lint-keyword-not-uppercase` | A clause keyword at the start of a line not written in uppercase. |
| `cypher-lint-multiple-match-per-line` | More than one `MATCH` on one line. |
| `cypher-lint-qpp-uncommented` | A quantified path pattern with no comment above it. |

## Testing

Graph tests run against a real Neo4j instance, never a mock. A mock answers what the test author
believes the database does, which is exactly the belief the test should check. Cover the empty,
single and many cases for each read, and assert row counts.

## Configurable rules

Every rule above that reads a `policy` register (or `constraints`, `common_label`, `properties`,
`relationships`) takes its facts from the schema file, `examples/schema.yaml` being the neutral
example. A rule whose register is empty fails closed or is off, as each section states.

One check is specified but not shipped: [every register entry binds to exactly one query
file](schema-specific/register-entries-bind-to-files.md). It holds the registers named in
[Where queries live](#where-queries-live) to the checked files, with input, algorithm and an
example.
