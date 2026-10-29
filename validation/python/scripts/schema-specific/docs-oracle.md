# Docs oracle: documentation tables as the source of truth

This is a specification, not shipped code. The pattern only pays off when a project's
architecture documents follow fixed templates, and the extractor has to know those
templates. Each adopter builds one for its own templates. The kit ships the parts that do not
depend on a template:

- the Markdown adapter (`pystatic/docs/markdown.py`)
- the frontmatter reader (`pystatic/docs/frontmatter.py`)
- the bundle checker (`pystatic.docs.check`)
- the consumers of an oracle's output: `enum-literals` (`values`), `config-literals`
  (`keys`), `runtime-defaults` (`defaults`), and the parity tests of the architecture
  track

## What it enforces

The documentation states every fact exactly once: tables, enums, configuration keys,
runtime defaults, error codes and wire shapes. Code and tests are then checked against that
statement, never against a second copy. The oracle is the only reader of the prose. It turns
the documents into one typed JSON file, and every check reads that file.

1. **Extraction is total.** Every row of every registered table becomes a typed record.
   - A missing required column is an error.
   - A malformed identifier cell is an error.
   - A second table where exactly one is expected is an error.
   - A row is never silently dropped.
2. **Errors are findings.** Each extraction error is printed as
   `path:line:1: docs-oracle-error: message (fix: ...; doc: /<file>#<anchor>)`. The run exits 1,
   but it still writes a best-effort JSON file, so downstream checks keep running.
3. **The output has a schema.** The JSON Schema of the output is written next to it, so a
   consumer written in another language can validate what it reads.

## Input

- A documentation bundle: Markdown files with frontmatter, one document per template.
- A **registry**, written by the adopter as data and not as parsing code. It has one entry
  per fact:

| Field | Meaning |
| --- | --- |
| `output` | Key in the output JSON, for example `sql_store.tables` |
| `document` | Bundle-relative path, for example `architecture/data-store.md` |
| `heading` / `heading_level` | The section that owns the table (`null`: every table in the document) |
| `required_heading` | Whether a missing section is an error or an empty result |
| `columns.key` / `columns.name_field` | The identifier column and the output field it becomes |
| `columns.required` | Column slugs every row must have |
| `columns.identifier_split` | One cell may name several identifiers (`a`, `b`): one record each |
| `columns.backtick` / `columns.verbatim` / `columns.refs` | Per-column cleanup: strip code marks, keep raw, or collect link targets |
| `columns.extra` | Derived fields computed from the raw row, such as `enum`, `unique` or `fk_table` |
| `empty_msg` | When set, an empty result is an error |

The registry may also hold list entries:

- **classified bullet list**: each item under a heading is turned into a record by a
  function.
- **labelled list**: a bold label paragraph followed by its bullet list.

## How to build it

1. Parse each registered document with `pystatic.docs.markdown`. That gives the headings
   with their spans, and the tables with their row lines. Blank the frontmatter first so
   line numbers stay correct.
2. For each entry, find the section by its normalised heading title. Then take the one
   table in it, or every table when the entry asks for that.
3. Turn each header cell into a slug: lower-case, with every run of non-alphanumeric
   characters replaced by `_`. Map column aliases to their canonical names.
4. For each row:
   1. Read the identifier from the `key` cell, with code marks and links removed. Split it
      when `identifier_split` is set.
   2. If the name still holds markup, record an error and skip the row.
   3. Clean the other cells as the column rules say.
   4. Add the `extra` fields and the source `line`.
5. Validate the assembled dictionary with Pydantic models. These models are frozen and
   use `extra="forbid"`, except on row models, which keep columns added later.
6. Write `build/docs-oracle.json`, sorted with a two-space indent. Write its JSON Schema
   (`model_json_schema()`) next to it.

## Consumers

| Consumer | Reads |
| --- | --- |
| `enum-literals` | Each `enum` value of the store columns, as `values`. Generate the `[enum-literals]` table, or a values file, from the JSON. |
| `config-literals` | The configuration keys, as `keys` |
| `runtime-defaults` | The numeric `default` of each runtime setting, as the `defaults` JSON (`SETTING_NAME: number`) |
| Parity tests (the architecture track) | Tables, columns, graph labels and error codes, compared with the code's own declarations |

## Example

The document `architecture/data-store.md` contains:

```markdown
## Orders

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid, unique | primary key |
| `status` | enum: `PENDING`, `SHIPPED`, `CANCELLED` | |
| `customer_id` | FK [customers](#customers) | |
```

The registry entry is
`TableEntry(output="tables.orders", heading="Orders", heading_level=2, columns=ColumnRule(key="column", required=("column", "type", "notes"), extra={"enum": enum_values, "unique": is_unique, "fk_table": fk_table}))`.

The output is:

```json
{"tables": {"orders": [
  {"name": "id", "line": 5, "type": "uuid, unique", "unique": true, "enum": null, "fk_table": null},
  {"name": "status", "line": 6, "enum": ["PENDING", "SHIPPED", "CANCELLED"], "unique": false, "fk_table": null},
  {"name": "customer_id", "line": 7, "fk_table": "customers", "unique": false, "enum": null}
]}}
```

`enum-literals` then receives `values = ["PENDING", "SHIPPED", "CANCELLED"]`. A test that
compares `order.status == "SHIPPED"` is reported.
