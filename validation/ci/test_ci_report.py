#!/usr/bin/env python3
"""Self-tests of ci_report.py: `env -u VIRTUAL_ENV uv run --project validation/python/lint pytest validation/ci`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ci_report
import pytest

KNIP_CLEAN = """\
== knip: knip --directory frontend --config frontend/knip.json --no-progress
Configuration hints (1)
uv    knip.json  Remove from ignoreBinaries

== summary
knip        pass
"""

KNIP_ISSUES = """\
== knip: knip --directory frontend --no-progress
Unused files (2)
lib/a.ts
lib/b.ts
Unused exports (1)
helper  function  lib/c.ts:3:14
Configuration hints (1)
uv    knip.json  Remove from ignoreBinaries

== summary
knip        fail (exit 1)
"""

RUN_ALL_CYPHER = """\
== cypher (/work/validation/cypher)
+ uv run cypher-checks all --config /work/backend/cypher-checks.toml
== lint
== parity
skipped: no `ddl` configured
== cyver
skipped: [live] enabled = false
== cypher: ok
== all selected tracks passed
"""

PYSTATIC_FINDINGS = """\
[file-size] ok (53 ms)
[registry-files] skipped (0 ms) -- skipped: no [[registry-files]] entry in the configuration
[docs-bundle] findings (130 ms)
[package-data] ok (187 ms)
python all: 4 steps, 2 findings, exit 1
design-system/README.md:1:1: docs-frontmatter: missing frontmatter (fix: add it; doc: /README.md#docs-bundle)
design-system/USAGE.md:1:1: docs-heading: non-ASCII heading (fix: rename it; doc: /README.md#docs-bundle)
"""

RUN_MJS_FINDINGS = """\
[tokens] ok
e2e/a.spec.ts:27:1: jscpd-clone: duplicates e2e/b.spec.ts:95

[duplicates] findings
2 steps, 1 findings, exit 1
"""

TWO_STEPS_UNNAMED = """\
[alpha] findings (1 ms)
[beta] findings (1 ms)
3 steps, 2 findings, exit 1
src/a.py:1:1: R1: first
src/b.py:2:1: R2: second
"""

SUBSTEP_LOGS = [KNIP_CLEAN, KNIP_ISSUES, RUN_ALL_CYPHER, PYSTATIC_FINDINGS, RUN_MJS_FINDINGS, TWO_STEPS_UNNAMED]

JUNIT = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="test_live" name="test_a" file="checks/test_live.py" line="3" time="0.5">
<skipped type="pytest.skip" message="the live service is not reachable"/></testcase>
<testcase classname="test_live" name="test_b" file="checks/test_live.py" line="9" time="0.25">
<skipped type="pytest.skip" message="the live service is not reachable"/></testcase>
<testcase classname="test_routes" name="test_c" file="checks/test_routes.py" line="4" time="0.1"/>
<testcase classname="test_routes" name="test_d" file="checks/test_routes.py" line="8" time="0.1">
<failure message="route /x has no contract">trace</failure></testcase>
<testcase classname="test_headers" name="test_e" file="checks/test_headers.py" line="2" time="0"/>
<testcase classname="test_headers" name="test_f" file="checks/test_headers.py" line="7" time="0">
<skipped message="the TLS probe needs a certificate"/></testcase>
</testsuite></testsuites>
"""

UNSAFE_XML = """\
<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<testsuites><testcase classname="t" name="&lol2;"/></testsuites>
"""


def _by_tool(rows: list[ci_report.Row]) -> dict[str, ci_report.Row]:
    return {str(row["step"]).rsplit(ci_report.SUBSTEP, 1)[-1]: row for row in rows}


# --------------------------------------------------------------------------- sub-steps of wrapper output


def test_knip_configuration_hints_are_not_findings() -> None:
    knip = _by_tool(ci_report.parse_substeps(KNIP_CLEAN, "typescript-lint"))["knip"]
    assert (knip["status"], knip["findings"]) == ("ok", 0)
    assert knip["detail"] == "1 configuration hint: uv knip.json Remove from ignoreBinaries"


def test_knip_issues_are_counted_without_the_hints() -> None:
    knip = _by_tool(ci_report.parse_substeps(KNIP_ISSUES, "typescript-lint"))["knip"]
    assert (knip["status"], knip["findings"]) == ("findings", 3)


def test_hints_treated_as_errors_are_the_findings() -> None:
    log = KNIP_CLEAN.replace("knip        pass", "knip        fail (exit 1)")
    knip = _by_tool(ci_report.parse_substeps(log, "typescript-lint"))["knip"]
    assert (knip["status"], knip["findings"]) == ("findings", 1)


def test_a_tool_with_findings_is_never_ok() -> None:
    log = "== mytool: run\nsrc/a.py:3:1: R100: bad thing\n\n== summary\nmytool      pass\n"
    tool = _by_tool(ci_report.parse_substeps(log, "track"))["mytool"]
    assert (tool["status"], tool["findings"]) == ("findings", 1)


@pytest.mark.parametrize("log", SUBSTEP_LOGS)
def test_no_row_is_ok_with_findings(log: str) -> None:
    for row in ci_report.parse_substeps(log, "track"):
        assert not (row["status"] == "ok" and row["findings"]), row
        assert not (row["status"] == "findings" and not row["findings"]), row


def test_findings_printed_after_every_marker_go_to_their_step() -> None:
    rows = _by_tool(ci_report.parse_substeps(PYSTATIC_FINDINGS, "python-scripts"))
    assert (rows["docs-bundle"]["status"], rows["docs-bundle"]["findings"]) == ("findings", 2)
    assert [rows[name]["findings"] for name in ("file-size", "registry-files", "package-data")] == [0, 0, 0]
    assert ci_report.runner_total(PYSTATIC_FINDINGS) == 2


PYSTATIC_CLONES = """\
[docs-bundle] findings (130 ms)
[clones] findings (9896 ms) -- 416 files, 2 clones, 0.15% duplicated lines
python all: 2 steps, 3 findings, exit 1
docs/a.md:1:1: docs-heading: non-ASCII heading (fix: rename it; doc: /README.md#docs-bundle)
tests/test_b.py:25:18: clones-duplicate: lines 25-52 duplicate tests/test_a.py:84-111 (266 tokens) \
(fix: move the shared code into one function, fixture or helper that both places use; doc: /README.md#clones)
src/b.py:7:1: clones-duplicate: lines 7-20 duplicate src/a.py:3-16 (120 tokens) \
(fix: move the shared code into one function, fixture or helper that both places use; doc: /README.md#clones)
"""


def test_the_python_clone_step_is_its_own_sub_step() -> None:
    rows = _by_tool(ci_report.parse_substeps(PYSTATIC_CLONES, "python-scripts"))
    clones = rows["clones"]
    assert clones["step"] == f"python-scripts{ci_report.SUBSTEP}clones"
    assert (clones["status"], clones["findings"], clones["duration_ms"]) == ("findings", 2, 9896)
    assert clones["detail"] == "416 files, 2 clones, 0.15% duplicated lines"
    assert rows["docs-bundle"]["findings"] == 1
    assert [row["path"] for row in ci_report.parse_findings(PYSTATIC_CLONES, "backend")][1:] == [
        "backend/tests/test_b.py",
        "backend/src/b.py",
    ]


def test_findings_printed_before_their_marker_stay_with_it() -> None:
    rows = _by_tool(ci_report.parse_substeps(RUN_MJS_FINDINGS, "typescript-scripts"))
    assert (rows["duplicates"]["status"], rows["duplicates"]["findings"]) == ("findings", 1)
    assert (rows["tokens"]["status"], rows["tokens"]["findings"]) == ("ok", 0)


def test_lines_no_step_can_take_leave_a_lower_bound_and_a_note() -> None:
    rows = _by_tool(ci_report.parse_substeps(TWO_STEPS_UNNAMED, "track"))
    for name in ("alpha", "beta"):
        assert (rows[name]["status"], rows[name]["findings"]) == ("findings", 1)
        assert rows[name]["detail"] == "at least 1 finding: 2 finding lines could not be given to one step"


def test_marker_count_is_used_when_no_line_is_printed() -> None:
    rows = _by_tool(ci_report.parse_substeps("==> sql-rules: findings (3)\n", "sql"))
    assert (rows["sql-rules"]["status"], rows["sql-rules"]["findings"]) == ("findings", 3)


def test_runner_total_is_read_only_from_bracket_logs() -> None:
    assert ci_report.runner_total("== lint\n8 steps, 3 findings, exit 1\n") is None


def test_run_all_banners_are_not_tools() -> None:
    rows = _by_tool(ci_report.parse_substeps(RUN_ALL_CYPHER, "cypher"))
    assert list(rows) == ["lint", "parity", "cyver"]
    assert rows["parity"]["detail"] == "skipped: no `ddl` configured"
    assert rows["cyver"]["detail"] == "skipped: [live] enabled = false"


def test_summary_only_steps_are_listed() -> None:
    log = "== tsc: tsc\n\n== summary\ntsc         pass\nsize-limit  skipped\nknip        not run (spawn ENOENT)\n"
    rows = _by_tool(ci_report.parse_substeps(log, "typescript-lint"))
    assert rows["size-limit"]["status"] == "skipped"
    assert rows["size-limit"]["detail"] == (
        "opt-in: run.mjs runs it only with --size-limit, which this command does not pass"
    )
    assert rows["knip"]["status"] == "tool-error"


def test_bracket_markers_keep_reason_and_duration() -> None:
    log = "[file-size] ok (52 ms)\n[json-schemas] skipped (0 ms) -- skipped: no entry\n"
    rows = _by_tool(ci_report.parse_substeps(log, "python-scripts"))
    assert rows["file-size"]["duration_ms"] == 52
    assert (rows["json-schemas"]["status"], rows["json-schemas"]["detail"]) == ("skipped", "skipped: no entry")


def test_arrow_state_with_colon_is_a_known_status() -> None:
    rows = _by_tool(ci_report.parse_substeps("==> sql-rules: findings: 2\n", "sql"))
    assert rows["sql-rules"]["status"] == "findings"


# --------------------------------------------------------------------------- JUnit


@pytest.fixture
def junit(tmp_path: Path) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT, encoding="utf-8")
    return path


def test_one_row_per_check_module_with_skip_reasons(junit: Path) -> None:
    rows = _by_tool(ci_report.junit_substeps(junit, "python-arch"))
    assert list(rows) == ["live", "routes", "headers"]
    assert (rows["live"]["status"], rows["live"]["detail"]) == ("skipped", "the live service is not reachable")
    assert rows["live"]["duration_ms"] == 750
    assert (rows["routes"]["status"], rows["routes"]["findings"], rows["routes"]["detail"]) == (
        "findings",
        1,
        "1 of 2 failed",
    )
    assert rows["headers"]["status"] == "ok"
    assert rows["headers"]["detail"] == "1 of 2 skipped: the TLS probe needs a certificate"


def test_junit_counts_and_failures(junit: Path) -> None:
    counts, failures = ci_report.read_junit(junit, junit.parent)
    assert counts == {"total": 6, "passed": 2, "failed": 1, "errors": 0, "skipped": 3}
    assert [(item["name"], item["message"], item["line"]) for item in failures] == [
        ("test_routes::test_d", "route /x has no contract", 8)
    ]


def test_entity_declarations_are_refused(junit: Path) -> None:
    junit.write_text(UNSAFE_XML, encoding="utf-8")
    with pytest.raises(ci_report.UnsafeXMLError):
        ci_report.read_junit(junit, junit.parent)


# --------------------------------------------------------------------------- the commands end to end


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / ".reports"
    monkeypatch.setattr(ci_report, "REPORTS", folder)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    return folder


def test_run_merge_and_summary(
    reports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "tool.py"
    script.write_text(f"import sys\nsys.stdout.write({KNIP_CLEAN!r})\n", encoding="utf-8")
    run = ["run", "--job", "lint", "--unit", "frontend", "--step", "typescript-lint", "--"]
    assert ci_report.main([*run, sys.executable, str(script)]) == ci_report.EXIT_OK
    skip = ["skip", "--job", "lint", "--unit", "frontend", "--step", "size-budget", "--reason", "no build"]
    assert ci_report.main(skip) == ci_report.EXIT_OK
    assert ci_report.main(["merge", "--expect", "lint"]) == ci_report.EXIT_OK
    assert (reports / "summary.json").is_file()
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    capsys.readouterr()
    assert ci_report.main(["github-summary"]) == ci_report.EXIT_OK
    markdown = capsys.readouterr().out
    knip_row = f"| frontend | typescript-lint{ci_report.SUBSTEP}knip | 1 | ✅ ok | 0 | 0.0 s | 1 configuration hint"
    assert knip_row in markdown
    assert "| frontend | size-budget | 1 | ⏭️ skipped | 0 | 0.0 s | no build |" in markdown
    assert f"Checked commit `{'a' * 12}`" in markdown


def test_bracket_runner_counts_reach_the_summary(
    reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "tool.py"
    script.write_text(f"import sys\nsys.stdout.write({PYSTATIC_FINDINGS!r})\nsys.exit(1)\n", encoding="utf-8")
    run = ["run", "--job", "static-python-scripts", "--unit", "backend", "--step", "python-scripts", "--"]
    assert ci_report.main([*run, sys.executable, str(script)]) == ci_report.EXIT_FINDINGS
    parent, *children = json.loads((reports / "static-python-scripts" / "summary.json").read_text(encoding="utf-8"))
    assert (parent["status"], parent["findings"]) == ("findings", 2)
    assert sum(child["findings"] for child in children) == 2
    assert ci_report.main(["merge", "--expect", "static-python-scripts"]) == ci_report.EXIT_FINDINGS
    capsys.readouterr()
    assert ci_report.main(["github-summary"]) == ci_report.EXIT_OK
    markdown = capsys.readouterr().out
    assert f"| backend | python-scripts{ci_report.SUBSTEP}docs-bundle | 1 | ⚠️ findings | 2 |" in markdown
    assert "| static-python-scripts | backend | python-scripts | ⚠️ findings | 2 |" in markdown
    assert "⚠️ findings | 0 |" not in markdown
    assert markdown.count("::warning ") == 2


def test_wrapper_with_findings_is_never_ok_despite_exit_zero(reports: Path, tmp_path: Path) -> None:
    script = tmp_path / "tool.py"
    script.write_text(f"import sys\nsys.stdout.write({RUN_MJS_FINDINGS!r})\n", encoding="utf-8")
    run = ["run", "--job", "scripts", "--unit", "frontend", "--step", "typescript-scripts", "--"]
    assert ci_report.main([*run, sys.executable, str(script)]) == ci_report.EXIT_FINDINGS
    parent = json.loads((reports / "scripts" / "summary.json").read_text(encoding="utf-8"))[0]
    assert (parent["status"], parent["findings"]) == ("findings", 1)


def test_junit_substeps_flag_lists_every_check(reports: Path, tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    script = tmp_path / "tool.py"
    script.write_text(f"from pathlib import Path\nPath({str(junit)!r}).write_text({JUNIT!r})\n", encoding="utf-8")
    argv = ["run", "--job", "arch", "--unit", "backend", "--step", "python-arch", "--junit", str(junit)]
    assert ci_report.main([*argv, "--junit-substeps", "--", sys.executable, str(script)]) == ci_report.EXIT_FINDINGS
    parent, *children = json.loads((reports / "arch" / "summary.json").read_text(encoding="utf-8"))
    assert (parent["status"], parent["children"], parent["findings"]) == ("failed", 3, 0)
    assert [child["parent"] for child in children] == ["python-arch"] * 3


# --------------------------------------------------------------------------- sharded jobs

PLAYWRIGHT_SHARD = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="{total}" failures="{failed}" skipped="0" errors="0">
<testsuite name="p02-foundation.spec.ts" hostname="chromium-1280" tests="{total}">
{cases}
</testsuite></testsuites>
"""


def _playwright_junit(passed: int, failed: int) -> str:
    cases = [f'<testcase name="[chromium-1280] ok {i}" classname="p02-foundation.spec.ts"/>' for i in range(passed)]
    cases += [
        f'<testcase name="[chromium-1280] bad {i}" classname="p02-foundation.spec.ts">'
        f'<failure message="expected visible" type="FAILURE">trace</failure></testcase>'
        for i in range(failed)
    ]
    return PLAYWRIGHT_SHARD.format(total=passed + failed, failed=failed, cases="\n".join(cases))


def _run_shard(tmp_path: Path, job: str, passed: int, failed: int) -> int:
    junit = tmp_path / f"{job}.xml"
    script = tmp_path / f"{job}.py"
    body = _playwright_junit(passed, failed)
    code = int(failed > 0)  # playwright test exits 1 when a test failed
    lines = [
        "import sys",
        "from pathlib import Path",
        f"Path({str(junit)!r}).write_text({body!r})",
        f"sys.exit({code})",
    ]
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    argv = ["run", "--job", job, "--unit", "frontend", "--step", "e2e-pack", "--junit", str(junit), "--"]
    return ci_report.main([*argv, sys.executable, str(script)])


def test_shard_names_are_parsed_and_ordered_by_number() -> None:
    assert ci_report.shard_of("e2e-pack-shard2") == ("e2e-pack", 2)
    assert ci_report.shard_of("e2e-pack-shard0") is None
    assert ci_report.shard_of("unit-python") is None
    jobs = ["e2e-pack-shard10", "e2e-pack-shard2", "e2e-build", "e2e-pack-shard1"]
    assert sorted(jobs, key=ci_report.job_key) == [
        "e2e-build",
        "e2e-pack-shard1",
        "e2e-pack-shard2",
        "e2e-pack-shard10",
    ]


def test_a_gap_in_the_shard_numbers_is_a_missing_report() -> None:
    jobs = ["e2e-pack-shard1", "e2e-pack-shard2", "e2e-pack-shard4", "e2e-production-shard2", "unit-python"]
    assert ci_report.missing_shards(jobs) == ["e2e-pack-shard3", "e2e-production-shard1"]
    assert ci_report.missing_shards(["e2e-pack-shard1", "unit-python"]) == []


def test_sharded_pack_reports_merge_into_one_combined_row(
    reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_shard(tmp_path, "e2e-pack-shard1", 3, 0) == ci_report.EXIT_OK
    assert _run_shard(tmp_path, "e2e-pack-shard2", 2, 1) == ci_report.EXIT_FINDINGS
    expect = ["merge", "--expect", "e2e-pack-shard1", "e2e-pack-shard2"]
    assert ci_report.main(expect) == ci_report.EXIT_FINDINGS
    capsys.readouterr()
    assert ci_report.main(["github-summary"]) == ci_report.EXIT_OK
    markdown = capsys.readouterr().out
    shard1 = "| e2e-pack-shard1 | frontend | e2e-pack | ✅ ok | 3 | 3 | 0 | 0 | 0 |"
    shard2 = "| e2e-pack-shard2 | frontend | e2e-pack | ❌ failed | 3 | 2 | 1 | 0 | 0 |"
    combined = "| **e2e-pack (2 shards)** | frontend | e2e-pack | ❌ failed | 6 | 5 | 1 | 0 | 0 |"
    assert shard1 in markdown
    assert shard2 in markdown
    assert combined in markdown
    assert markdown.index(shard1) < markdown.index(shard2) < markdown.index(combined)
    assert "**frontend / e2e-pack** (e2e-pack-shard2)" in markdown
    assert "| **Total** | | | ❌ failed | **0** | **5 / 6** |" in markdown


def test_a_shard_that_sent_no_report_fails_the_merge(reports: Path, tmp_path: Path) -> None:
    assert _run_shard(tmp_path, "e2e-pack-shard1", 1, 0) == ci_report.EXIT_OK
    assert _run_shard(tmp_path, "e2e-pack-shard3", 1, 0) == ci_report.EXIT_OK
    assert ci_report.main(["merge"]) == ci_report.EXIT_TOOL_ERROR
    rows = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    assert [row["detail"] for row in rows if row["step"] == "missing-reports"] == ["no report from: e2e-pack-shard2"]
    assert ci_report.main(["merge", "--expect", "e2e-pack-shard4"]) == ci_report.EXIT_TOOL_ERROR
    rows = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    missing = [row["detail"] for row in rows if row["step"] == "missing-reports"]
    assert missing == ["no report from: e2e-pack-shard2, e2e-pack-shard4"]


def test_summary_without_merge_is_a_tool_error(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert not reports.exists()
    assert ci_report.main(["github-summary"]) == ci_report.EXIT_OK
    assert "tool-error" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
