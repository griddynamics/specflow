# CI validation report for GitHub Actions

`ci_report.py` is one stdlib-only Python script (3.10+) that turns the results of many validation
jobs into one GitHub Actions run summary: a table of test packs, a table of checks, failing tests
and tool errors listed in place, and file/line annotations on the pull request. It works with any
command that has an exit code; tools that write **SARIF** (linters, semgrep) or **JUnit XML** (test
runners) add per-finding and per-test detail. Nothing in it names a project: copy this folder
with the rest of the kit.

## Commands

```text
ci_report.py run   --job JOB --unit UNIT --step STEP [--cwd DIR] [--sarif F] [--junit F]
                   [--junit-substeps] [--ok-codes N...] [--findings-codes N...] -- CMD ...
ci_report.py skip  --job JOB --unit UNIT --step STEP --reason TEXT
ci_report.py merge [--expect JOB ...] [--plan FILE]
ci_report.py github-summary [--max-annotations N]
```

| Command | Runs in | Does |
|---|---|---|
| `run` | every check or test step | Runs the command, tees its output, records exit code, duration and output; reads the SARIF/JUnit files you name; exits 0, 1 or 2 so the step still fails the way it should. |
| `skip` | a step left out on purpose | Records a `skipped` row with a reason. |
| `merge` | the gate job | Merges every job's `summary.json` into `.reports/summary.json`, reports expected jobs that sent nothing, exits with the worst status. |
| `github-summary` | the gate job, after `merge` | Writes the Markdown to `$GITHUB_STEP_SUMMARY` (stdout when unset) and prints `::error` / `::warning` / `::notice` annotations. |

Exit codes: **0** ok, **1** findings or failing tests, **2** tool or configuration error. Statuses rank
`ok < skipped < findings < failed < tool-error`. A tool whose exit code means something else declares it
(`--ok-codes 0 5`, `--findings-codes 1 2`; `make` exits 2 when a recipe fails).

## Per-tool rows

A wrapper command such as `make validate-python-lint` runs many tools. `run` splits its output into one
row per tool (step `python-lint › ruff`) with a finding count, and the parent row carries the sum (or the runner's own total, when it prints one). It
understands the markers printed by the kit runners (`==> name: ...`, `== name: ...` with a `== summary`
table, `[name] status`). The count comes from the tool's own summary line (ruff, mypy, semgrep, deptry,
import-linter, eslint, stylelint, prettier, knip), else from its `path:line:col: rule: message` lines.
Those lines also become file annotations; `--path-prefix backend` makes their paths repo-relative.

- A tool whose output holds findings is never `ok`: a positive count turns its row into `findings`,
  and a wrapper with a `findings` tool row is `findings` too, even when it exited 0.
- A row is never `findings` with 0 findings. The bracket runners (pystatic, `run.mjs`) print the
  findings apart from the markers: pystatic prints all of them after the last marker, `run.mjs`
  prints each step's findings before its marker. Each `path:line:col: rule: message` line goes to the
  step named by its `doc: /README.md#<step>` anchor, else the step its rule starts with, else the
  marker after it, else the one step whose marker says `findings`. The parent row takes the runner's
  own total (`python all: 13 steps, 92 findings, exit 1`). A `==> name: findings (3)` marker gives
  its own count. When a step reports findings and none of its lines can be found, its count is 1
  (the lower bound), and its notes say why.
- knip's `Configuration hints (N)` are advice about knip's own config, printed on a clean run: they
  are not counted, and their text goes to the row's notes (with `--treat-config-hints-as-errors`,
  knip fails and the hints become the count).
- A step named only in a runner's `== summary` table (for example `size-limit  skipped`, which never
  printed a section) still gets a row; `not run (...)` there is a `tool-error`. A skipped one's notes
  give the runner's reason when it is known (`size-limit`: opt-in, run only with `--size-limit`;
  `tsc`: run only with a `tsconfig.json`), else say that the step did not run.
- `validation/run-all.sh` banners (`== <track> (dir)`, `== <track>: ok`, `== all selected tracks
  passed`) are not tools.
- `--junit-substeps` adds one row per test module of the JUnit files: for a pytest-based checker
  such as archkit, every check is listed, with its failing count (or a module's pytest skip
  reason, when it has one). The parent row keeps its own status and test counts.

The summary's checks table has a **Notes** column: a skipped step's reason, a tool's hints, or
`exit N` of a failing wrapper. The summary also names the commit that was checked (on a pull request,
GitHub's merge of the head into the base): a local run that differs usually sees uncommitted files.

JUnit files are parsed with expat and any DTD or entity declaration is refused (no entity
expansion), so the script stays stdlib-only without `xml.etree` parsing untrusted input.

## Reports

Everything lives under `$CI_REPORTS_DIR` (default `.reports`), one folder per job, so artifacts of
parallel jobs merge without collisions:

```text
.reports/
  plan.json                     optional: {"expected_jobs": ["lint", "unit-1", ...]}
  summary.json                  written by `merge`
  <job>/summary.json            rows appended by `run` / `skip`
  <job>/<unit>/<step>.json|txt  one step's full report and raw output
```

## Wiring (see `.github/workflows/validate.yaml` of this repository)

1. Wrap each check or test command:
   `python3 validation/ci/ci_report.py run --job <job> --unit <unit> --step <step> -- <command>`.
   Give each matrix instance its own `--job`; `--sarif` / `--junit` paths are relative to `--cwd`.
2. Upload `.reports/` as artifact `validation-reports-<job>-<attempt>` with `if: always()`
   (`include-hidden-files: true`).
3. A gate job (`if: always()`, `needs:` every job) downloads `validation-reports-*` with
   `merge-multiple: true`, runs `merge --expect <every job>` (with `continue-on-error`), then
   `github-summary`, then fails if any `needs` result or the merge was not successful.

## Sharded test packs

A pack split over a matrix (`playwright test --shard=i/N`) reports one job per shard, named
`<pack>-shard<i>` (`--job e2e-production-shard2`), all with the same `--unit` and `--step`:

- jobs are ordered by shard number everywhere (`shard2` before `shard10`);
- `merge` names every shard below the highest one that reported and sent nothing (shards 1, 2 and 4
  reported: `shard3` is missing), on top of the `--expect` list, which should still name every shard
  (`--expect e2e-production-shard1 ... e2e-production-shard2`), so a last shard that died is caught too;
- the test packs table lists each shard and then one bold combined row, `<pack> (N shards)`, with the
  summed counts, the worst status and the slowest shard's duration (the shards run in parallel).
  The overview keeps one row per shard and adds no combined row, so its totals count every test once.

## Getting SARIF and JUnit out of common tools

| Tool | Command | Pass to `run` |
|---|---|---|
| ruff | `ruff check --output-format sarif --output-file ruff.sarif .` | `--sarif ruff.sarif` |
| semgrep | `semgrep scan --config <rules> --error --sarif --output semgrep.sarif` | `--sarif semgrep.sarif` |
| ESLint | `eslint --format @microsoft/eslint-formatter-sarif --output-file eslint.sarif .` | `--sarif eslint.sarif` |
| pytest | `pytest --junitxml=junit.xml -o junit_family=xunit1` | `--junit junit.xml` |
| Vitest | `vitest run --reporter=default --reporter=junit --outputFile.junit=junit.xml` | `--junit junit.xml` |
| Playwright | `PLAYWRIGHT_JUNIT_OUTPUT_NAME=junit.xml npx playwright test --reporter=line,junit` | `--junit junit.xml` |
| node:test | `node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination=junit.xml` | `--junit junit.xml` |
| anything else | the plain command; exit code and output are recorded | none |

## Local preview

```sh
python3 validation/ci/ci_report.py run --job local --unit api --step pytest --junit junit.xml -- pytest --junitxml=junit.xml
python3 validation/ci/ci_report.py merge && python3 validation/ci/ci_report.py github-summary
```

## Self-tests

`test_ci_report.py` (pytest, from the python-lint kit's environment): `make validate-ci-selftest`,
also run by `make validate-selftest` and by the `python-lint` job of `validate.yaml`.

`test_run_all.py` runs the kit's track runner `../run-all.sh` in the same pytest session: its
options, usage errors (exit 2), `--list`, `--skip` and the visible skip of a track whose input
file is missing. It starts no track tool (no uv, npm or Docker). These tests make this folder
the source-ownership owner of `run-all.sh` (the repository root `pystatic.toml`).
