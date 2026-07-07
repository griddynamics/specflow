"""Canonical filenames for SpecFlow artifacts.

Single source of truth for the file names the local MCP tools
(`check_specification_completeness`, `run_planning`) must produce and the backend
contract validator expects.
See CLAUDE.md "File/Directory Contract".
"""

SPEC_INDEX_FILE = "specification_index.md"
REPO_SUMMARY_FILE = "repo_summary.md"
SPEC_COMPLETENESS_FILE = "specification_completeness.md"
IMPLEMENTATION_PLAN_FILE = "IMPLEMENTATION_PLAN.md"
E2E_TEST_PLAN_FILE = "e2e-test-plan.md"

# P10Y multi-workspace estimation report — written by the estimation workflow
# under ARTIFACTS_BASE/{generation_id}/report/, read back by the report-html
# API endpoint and the local TUI.
MULTI_WORKSPACE_REPORT_MD_FILE = "multi-workspace-estimation-report.md"
MULTI_WORKSPACE_REPORT_HTML_FILE = "multi-workspace-estimation-report.html"
