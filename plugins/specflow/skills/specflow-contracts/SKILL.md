---
name: specflow-contracts
description: Generate the data model and API contract from your spec as real schemas (SQL DDL and OpenAPI JSON), then validate them mechanically. Spec contradictions usually surface as structural impossibilities.
argument-hint: "(optional) spec_dir outputs_dir — defaults: specs docs"
---

# SpecFlow Contracts

Emit the data model and API contract the spec implies as **real artifacts**, then
run real checks on them.

This keeps the compiler and drops the application. A build gives you an oracle
that does not care what anyone believes — the schema either holds together or it
does not. You can keep most of that at no runtime cost, because an
underdetermined spec tends to surface as a *structural impossibility*: a field
that must be both supplied and computed, two entities that each require the
other to exist first, an endpoint returning a shape nothing defines. Nobody
writes those deliberately.

```bash
SF="${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib/specflow_cli.py"
```

## What to do

### 1. Make sure there is a model to work from

The checks run against a lens artifact. If `<outputs_dir>/refine/` has no rounds,
run `/specflow-simulate` first (or `/specflow-refine` for the full loop) — this
skill validates a model, it does not invent one.

### 2. Check the model for contradictions

```bash
python3 "$SF" contracts --outputs docs
```

This needs no emitted files. It reports:

| Issue | Why it matters |
|---|---|
| `contradiction` | a field both `required` and `derived` — the spec never says who supplies it |
| `circular-requirement` | two entities each requiring a reference to the other; neither can be created first |
| `dangling-reference` | a foreign key to an entity that does not exist |
| `no-identity` | an entity with no primary key — its rows cannot be addressed |
| `unknown-field` | an operation reading or writing a field the entity does not have |
| `unguarded-mutation` | an operation that changes data with no stated authorization |

Each one is a concrete spec gap. Report them as such, with the requirement they
trace back to.

### 3. Emit the artifacts

Write into `<outputs_dir>/refine/contracts/`:

- **`schema.sql`** — `CREATE TABLE` per entity. Real types, primary keys,
  foreign keys with explicit `REFERENCES`, `NOT NULL` where the model says
  required.
- **`api.json`** — OpenAPI **as JSON, not YAML**. One path per operation, request
  and response schemas under `components.schemas`, every `$ref` resolving inside
  the document.

Emit what the model actually says, including the parts you think are wrong. The
purpose is to expose contradictions, so smoothing them over while writing
defeats it. If you cannot emit something because the spec contradicts itself,
that is the finding — report it rather than inventing a resolution.

JSON rather than YAML is deliberate: it validates with the standard library, so
this skill needs no `pip install`.

### 4. Cross-check what you emitted

```bash
python3 "$SF" contracts --outputs docs \
  --sql docs/refine/contracts/schema.sql \
  --api docs/refine/contracts/api.json
```

This catches drift between the model and the artifacts — a missing table, a
dangling foreign key, a table with no primary key, an unresolvable `$ref`, an
untyped property, or operations the API never exposes.

Non-zero exit means something does not hold together. Report it; do not paper
over it by editing the artifact until the check passes.

### 5. Report

Lead with the contradictions, because those are spec defects rather than
modelling choices. Then say what you emitted and where.

If everything passes, say what that does and does not prove: the model is
internally consistent and the contracts match it. It does not prove the spec
describes what the user wants, and it does not prove the system would work.
A consistent model of the wrong thing still validates cleanly.
