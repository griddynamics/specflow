# pystatic rule catalogue

Every rule id the kit can report, grouped by check. A finding prints as
`path:line:col: rule: message (fix: ...; doc: ...)`. Options and defaults for each check are in
[README.md](README.md); this file says what each rule enforces and why.

Checks marked "config" read their facts from `pystatic.toml`
(see [Configurable rules](README.md#configurable-rules)).

## Index

| Check | Rule ids | Input |
| --- | --- | --- |
| blocking-in-async | `blocking-in-async` | config (optional) |
| no-dict-schemas | `no-dict-schemas` | config |
| enum-literals | `enum-literals-hand-typed` | config |
| config-literals | `config-literals-numeric`, `config-literals-class-default`, `config-literals-class-no-config` | config |
| registry-files | `registry-files-missing`, `registry-files-stray` | config |
| config-scoping | `no-suppressions-*` | reviewed table |
| json-schemas | `json-schemas-invalid`, `-unparseable`, `-duplicate-key`, `-file-name`, `-missing-schema` | config |
| deployment-files | `deployment-test-input`, `deployment-test-mount`, `ci-full-history-blobs` | config |
| file-size | `file-size-over-ceiling`, `file-size-over-page-budget`, `file-size-pages-not-countable` | config |
| source-ownership | `source-without-validation-owner` | config |
| runtime-defaults | `runtime-defaults-hard-coded` | config |
| docs-bundle | `docs-frontmatter`, `docs-numbered-record`, `docs-heading`, `docs-duplicate-id`, `docs-id-placement`, `docs-ephemeral`, `docs-link`, `docs-index-stale` | config |
| package-data | `package-data-missing-from-wheel`, `package-data-test-support`, `package-data-build-failed` | config |
| openapi-snapshot | `openapi-snapshot-stale` | CLI options |
| length-limits | `pylint-too-many-lines`, `comment-length`, `pep695-generic-syntax`, `pylint-<symbol>` | none |
| dead-code | `vulture-dead-code` | `pyproject.toml` scopes |
| clones | `clones-duplicate` | config |
| clone-inputs | none (a filter) | stdin |
| collected-docs | none (a pytest helper) | none |

## blocking-in-async

Rule `blocking-in-async`. Flags a known-blocking library call made directly in an `async def`
body: pandas and openpyxl readers, and synchronous cloud-SDK client constructors and I/O
methods. The call set is configurable: `calls` replaces it, `extra-calls` extends it. Why: a blocking call stalls the event loop and every request on
it. Ruff's `ASYNC` rules cover the standard library; this covers library calls. Wrapping the
call in `asyncio.to_thread`, `anyio.to_thread.run_sync` or `run_in_threadpool` is allowed;
nested `def` and lambda bodies are not checked.

```python
async def load(path):
    return pandas.read_excel(path)  # bad
    return await asyncio.to_thread(pandas.read_excel, path)  # good
```

## no-dict-schemas

Rule `no-dict-schemas`. A response-schema class (name ends in `Out`, `Response`, `Result`,
`Row`, `DTO`, `View`, or your
`suffixes`) must not declare a field typed `dict[str, Any]`, `dict[str, object]` or
`dict[str, str]`. Why: an untyped map hides the wire shape from the generated API contract
and from clients. Options: `scan`, `suffixes`, `open-fields` (fields the contract declares
open).

```python
class OrderOut(BaseModel):
    totals: dict[str, Any]  # bad
    totals: OrderTotals  # good
```

## enum-literals

Rule `enum-literals-hand-typed`. A string literal equal to a configured enum value, compared
with, assigned to, or passed as a keyword to one of the names `status`, `kind`, `role`,
`stage`, `outcome`, `mode`, `action`, `tier` (replace with `names`). Why: a retyped value
drifts silently when the enum changes; the member is the single source. A keyword is accepted
when the callee declares that field as a `Literal[...]` containing the value. Assignments in
an `Enum` body are exempt. Input: `values` and/or `values-from-enums`, `names`,
`import-roots`.

```python
if order.status == "SHIPPED":
    ...  # bad
if order.status == Status.SHIPPED:
    ...  # good
```

## config-literals

Rules `config-literals-numeric`, `config-literals-class-default`,
`config-literals-class-no-config`. In the configured `scan` directories, production code may
use only the numbers in `allowed` (default 0, 1, -1), except the value of an `UPPER_SNAKE`
constant whose lower-case name is in `keys`, subscript positions and Pydantic
`Field(ge/gt/le/lt)` bounds. A class in `config-classes` has no field defaults and is never
constructed with no arguments. Why: tunable behaviour must come from configuration, not from
a number buried in code, and a config-only class with defaults would hide a missing setting.

```python
discount = price * 0.15  # bad
discount = price * cfg.max_discount  # good
```

## registry-files

Rules `registry-files-missing`, `registry-files-stray`. Every member of an enum (read by AST
from `enum = "path.py:Class"`) owns exactly one file in `directory` named by `pattern`
(`{value}.md` default, or `{name}`), and nothing else is there. Why: a registry that is
partly on disk and partly in code drifts: members without files fail at run time, stray or
versioned copies are dead weight. `exclude` lists members with no file; `ignore` lists
directory entries to skip.

## config-scoping

Rules `no-suppressions-ruff-global-ignore`, `-ruff-per-file-ignore`, `-ruff-exclude`,
`-mypy-ignore-errors`, `-mypy-disable-error-code`, `-mypy-ignore-missing-imports`,
`-mypy-exclude`, `-pytest-filterwarnings`, and `no-suppressions-stale-review`. Each
suppression in `pyproject.toml` (ruff ignores, per-file ignores and excludes; mypy
`ignore_errors`, `disable_error_code`, `ignore_missing_imports`, `exclude`; pytest
`filterwarnings` starting with `ignore:`) must be listed with a reason in the reviewed table.
A reviewed row the config no longer holds is stale. Why: suppressions accumulate silently;
forcing a written reason keeps each one deliberate and the table shrinks as they are fixed.
Format: `reviewed-suppressions.example.toml`.

## json-schemas

Rules `json-schemas-invalid` (violates its schema, via `check-jsonschema`),
`json-schemas-unparseable`, `json-schemas-duplicate-key` (an object repeats a key, which
`json.loads` hides), `json-schemas-file-name` (file not named `<lower-cased name-field>.json`)
and `json-schemas-missing-schema`. Why: committed data documents need a contract, and
duplicate keys and misnamed files are otherwise invisible. Entry: `schema`, `files`,
`name-field`.

## deployment-files

- `deployment-test-input`: a production Compose service sets an environment key naming a test
  or seed input (a word in `input-words`, or a `replay-words` word plus a `locator-words`
  word). `allowed-keys` exempts keys.
- `deployment-test-mount`: a production Compose file takes a volume, `env_file`, secret or
  config from a `forbidden-trees` directory.
- `ci-full-history-blobs`: an `actions/checkout` step with `fetch-depth: 0` and no
  `filter: blob:none`.

Why: production must not depend on test data, and full-history CI checkouts download every
historical blob.

```yaml
- uses: actions/checkout@v4
  with: {fetch-depth: 0}                       # bad
- uses: actions/checkout@v4
  with: {fetch-depth: 0, filter: "blob:none"}  # good
```

## file-size

Rule `file-size-over-ceiling`. A tracked or untracked-but-not-ignored file
(`git ls-files --cached --others --exclude-standard`) under `paths` larger than `max-bytes`
(default 10 MiB). Why: oversized fixtures and data bloat the repository and every clone.

Rule `file-size-over-page-budget`. A PDF or PPTX under `page-paths` with more than `max-pages`
(default 2) pages or slides. Rule `file-size-pages-not-countable`. Any other file under
`page-paths`. Why: test inputs stay excerpts, and a document whose pages cannot be counted
escapes the budget. `suffixes` limits both rules to those extensions.

## source-ownership

Rule `source-without-validation-owner`. A tracked or untracked-but-not-ignored source file (by `extensions`) outside every
directory in `projects`. A project claims outside files with `[tool.source-ownership] external`
globs in its `pyproject.toml`. Why: a source no validation covers is unchecked code.

## runtime-defaults

Rule `runtime-defaults-hard-coded`. In `scan` directories, a numeric literal equal to a
setting's default whose name appears within five lines (`SETTING_NAME`, `setting_name`,
`settingName`). Quoted override keys, comments, docstrings, 0 and 1 are exempt. Why: tests
that repeat a default number break silently when the default changes; read it from the
generated defaults file.

## docs-bundle

Rules for a Markdown bundle (conventions from `[docs-bundle]`):

| Rule | Enforces |
| --- | --- |
| `docs-frontmatter` | Required and optional fields, allowed types and statuses, per-folder types, tags resolving to tag-folder documents, tagged reading-order links, no frontmatter on non-root indexes |
| `docs-numbered-record` | Numbered records match `filename`, and the first heading's slug equals the file stem |
| `docs-heading` | No numbered, non-ASCII or forbidden-character headings, no duplicate slugs per file |
| `docs-duplicate-id` | An identifier is declared in only one table row across the bundle |
| `docs-id-placement` | Identifier rows stay out of their forbidden folders |
| `docs-ephemeral` | No work-in-progress markers (status glyphs, code citations, to-dos) outside code fences |
| `docs-link` | Absolute internal links resolve to a file and heading slug |
| `docs-index-stale` | Generated `index.md` files match `pystatic.docs.indexes` |

Why: documentation that is the source of truth must be well-formed, unambiguous and free of
transient notes. `python -m pystatic.docs.indexes --root DIR` regenerates the indexes.

## package-data

Rules `package-data-missing-from-wheel`, `package-data-test-support`,
`package-data-build-failed`. Builds a wheel and checks that every data file under
`package-dir` with a `data-suffixes` suffix is inside it, and that no test support
(`test_*`, `conftest`, `fixtures`, `fixture_*`) ships. Why: a file present in the tree but
absent from the wheel works in development and fails when installed.

## openapi-snapshot

Rule `openapi-snapshot-stale` (with `--check`). Writes the app's OpenAPI document as
deterministic JSON (sorted keys, two-space indent, trailing newline). `--check` fails when the
committed snapshot differs. Why: contract drift becomes a reviewable diff. Options:
`--app MODULE:ATTR`, `--out`, `--check`, `--secret-env NAME`.

## length-limits

- `pylint-too-many-lines`: a production module over 3000 lines (tests and fixtures exempt).
- `pylint-<symbol>`: a pylint pragma that would suppress that check
  (`locally-disabled`, `file-ignored`, `suppressed-message`).
- `comment-length`: a comment block or docstring over 5 lines.
- `pep695-generic-syntax`: `def f[T]`, `class C[T]` or `type X = ...`. Why: that syntax
  stops semgrep from matching `async def` patterns in the same file; use `TypeVar` or
  `TypeAliasType`.

Why the limits: oversized modules and essay-length comments are a maintenance cost; split the
module or move the prose to documentation.

## dead-code

Rule `vulture-dead-code`. Printed as `ABSOLUTE_PATH:LINE:1: vulture-dead-code: MESSAGE` (no fix or
doc tail), unlike the other checks. vulture over the `[[tool.dead-code.scopes]]` in `pyproject.toml`: `sources`
own findings, `consumers` supply usage, `exclude` removes paths. Framework entry points
(routes, validators, fixtures, hooks, `override`, `visit_*`, data-class fields, Alembic
identifiers) are not reported. Why: unused code is untested and misleading.

## clones

Rule `clones-duplicate`. A Python fragment of at least `min-lines` lines and `min-tokens`
tokens (jscpd, `weak` mode by default) repeats code found earlier in the same or another
file. Imports never count, and in test files neither do independent assertion oracles
(see clone-inputs), so only copied implementation and copied setup are reported. Why: a copy
must be fixed twice and drifts; zero baseline, so `threshold` is always 0. Fix by moving the
shared code into one function, fixture or helper.

```python
# tests/test_orders.py and tests/test_refunds.py
def test_it(db):
    db.add(Customer(id=1)); db.add(Order(id=7, customer_id=1)); db.commit()  # bad: copied in both
def test_it(order_in_db):  # good: one fixture in conftest.py builds it
```

## clone-inputs

Not a rule: a stdin to stdout filter for clone detectors such as jscpd. Given a JSON list of
paths, it prints a JSON object mapping each path to the lines to omit (imports and pure
assertion oracles). Why: independent assertions should not count as duplicated code, while
duplicated setup still does.

## collected-docs

Not a rule: `pystatic.harness.collected_docs.citation_problems` returns the test modules that
declare no `ANCHOR` or `DOC_ANCHORS` and the anchors that resolve to no heading. Why: every
test module should cite the documentation it verifies.

## Runner and configuration files

| File | Role |
| --- | --- |
| `pystatic/run.py` | Runs 13 quick steps (every check above except package-data, openapi-snapshot, length-limits, dead-code, clone-inputs, collected-docs), plus `package-data` at `--level all`. Exit is the worst step's code |
| `pystatic.example.toml` | One table per configurable check, neutral example values |
| `reviewed-suppressions.example.toml` | Reviewed-suppression table template |
| `reviewed-suppressions.toml` | The kit's own reviewed rows |
| `pyproject.toml` | Pinned dependencies and the strict ruff, mypy, pytest, coverage and deptry settings |
| `schema-specific/docs-oracle.md` | Spec for deriving check inputs from documentation tables |
