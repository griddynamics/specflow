---
name: specflow-analysis
description: Analyze spec completeness locally — gap detection across every architectural dimension. Emits a human report plus a machine-checkable dimensions file. No backend, nothing leaves the machine, repeatable as specs evolve.
argument-hint: "(optional) spec_dir outputs_dir src_dir — defaults: specs docs src"
---

# SpecFlow Analysis

You are a software architect analyzing whether a specification determines enough
to build from. Read everything in `spec_dir`, then lock every architectural
dimension to exactly one value — and where the spec does not determine one, say
so rather than choosing quietly.

```bash
SF="${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib/specflow_cli.py"
```

## Arguments

- `spec_dir` — default `specs`
- `outputs_dir` — default `docs`
- `src_dir` — existing code, if any. Default `src`.

## The dimensions framework lives in the schema

`lib/specflow/schema/dimensions.schema.json` in this plugin is the **single
source of truth** for what must be decided. Read it before you start. It defines:

- **Part A** — six universal dimensions, mandatory for every project, each locked
  to exactly one value: persistence, infrastructure complexity, scale target,
  technology stack, quality level, scope boundaries.
- **Part B** — technology-specific dimensions, by project type.
- **Part C** — project-specific dimensions you discover. These are the variance
  sources the framework did not anticipate, and they are often the most valuable
  part of an analysis.
- **Part D** — micro-level consistency locks: naming conventions and code
  patterns. All fields required. These are the values that silently diverge
  between two independent implementations of the same spec, which is exactly why
  they are pinned here.

Keeping this in a schema rather than in prose means the completeness of your
output is *checked* rather than trusted.

## What to do

### 1. Read the spec properly

If the spec tree is large, build an index first — file, purpose, and the
requirements each file carries — and write it to
`<outputs_dir>/analysis/specification_index.md`. Work from the index rather than
re-reading everything repeatedly.

Read `src_dir` if it exists. Existing code is evidence about intent and sometimes
settles a dimension the prose leaves open. Where code and spec disagree, that is
itself a finding.

### 2. Lock every dimension

For each dimension in the schema, record the value **and where it came from**.
Every value carries a `spec_anchor`: the file, and the section if you can name
one.

When the spec does not determine a value:

- set `"inferred": true` on that anchor, and
- state the gap explicitly in the report.

Do not fill a cell with `TBD`, `unknown`, `varies`, or `N/A`. Those are rejected
mechanically, for a good reason: an evasion looks filled while telling the reader
nothing. If you cannot determine a value, the gap *is* the finding.

### 3. Write both outputs

**`<outputs_dir>/analysis/specification_completeness.md`** — the human report:

- what the spec determines, dimension by dimension,
- what it does not, with the specific requirement each gap belongs to,
- contradictions, where two parts of the spec cannot both hold,
- what you would need to ask to close each gap.

**`<outputs_dir>/analysis/dimensions.json`** — the same values in the schema's
shape. This is what makes the analysis comparable and checkable rather than
merely readable.

### 4. Check your own work

```bash
python3 - <<PY
import json, sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT:-$(pwd)/plugins/specflow}/lib")
from specflow.jsonschema_mini import validate_as
result = validate_as(json.load(open("docs/analysis/dimensions.json")), "specflow/dimensions")
print("\n".join(str(p) for p in result.problems) or "dimensions OK")
sys.exit(0 if result.ok else 1)
PY
```

Fix whatever it reports before finishing. Do not describe the analysis as
complete while the check fails.

## Report honestly

Lead with what is missing, not with what is present — the gaps are why the user
ran this.

Be specific about each one: which requirement, what decision is absent, what
would settle it. "The spec is vague about persistence" is not useful.
"Requirement 3.2 assumes records survive a restart but no storage is named" is.

Say what this skill does **not** do. It reads the spec; it does not simulate
building from it. So it finds the gaps visible on careful reading and misses the
ones that only appear when everything has to be made concrete at once. If the
spec looks broadly complete here, `/specflow-refine` is the next step — it runs
independent readings against each other and finds what a single careful pass
cannot.
