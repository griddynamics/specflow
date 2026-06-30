"""FastMCP server instructions string."""

SERVICE_DESCRIPTION = "SpecFlow MCP - client to interact with a remote AI Agent Harness for Full-Stack Code Generation"

SUPPORTED_CODING_AGENTS = ["cursor", "claude-code"]

SERVER_INSTRUCTIONS = """
    This server is an Agent Harness that automates the generation, deployment, and testing of full-stack codebases.

    WORKFLOW:

    1. Local (no backend) — user runs MCP tools in their IDE:
         check_specification_completeness  → agent follows returned template; writes
                                             {outputs_dir}/analysis/specification_completeness.md
         run_planning  → agent follows returned template; writes
                         {outputs_dir}/planning/IMPLEMENTATION_PLAN.md
                              (and e2e-test-plan.md if analysis says INTEGRATION_TESTS_READY)
       Both only return instruction templates — no upload, no session. Repeatable anytime.

    2. Backend — user runs `run_generation` once:
         Validates the local files, uploads them, then runs 2–8 hours of autonomous codegen
         and (optionally) deploy + E2E. Emails USER_EMAIL when done.
         If any required file is missing, refuses immediately with a message naming
         the file and which MCP tool to re-run.

    TOOL CALL POLICY:
    - Every tool requires explicit user instruction — never call any tool proactively.
    - run_generation RETURNS IMMEDIATELY; the work runs in the background.
      ⛔ DO NOT start a polling loop. DO NOT call check_status on a timer.
    - check_status: only when the user explicitly asks "what's the status?".
    - download_outputs: only when the user asks for the files.
    - run_generation: never call more than once per user request; never while already running.
    - ⛔ DO NOT AUTO-RETRY on timeout — the operation may have started. Ask the user.

    SESSION:
    - generation_id is saved to specflow_session.json (project root). Reused across restarts.
    - ⛔ NEVER delete specflow_session.json on your own. Only when the user requests a new
      session and the current workflow is terminal (completed/failed).
    - Multiple IDE windows share one API key up to its concurrent-session limit (default 5).

    ENVIRONMENT VARIABLES (set in MCP config "env" block):
    - BACKEND_URL: Backend API endpoint (default: http://127.0.0.1:8000)
    - SPECFLOW_API_KEY: API authentication key (hosted mode only — omit for local mode)
    - USER_EMAIL: Notification email for completed runs
    - WORKSPACE_COUNT: Parallel agents per run — 1, 2, or 3 (default: 3).
    - LLM_HIGH / LLM_MEDIUM / LLM_LOW: Model overrides (e.g. "anthropic/claude-opus-4.5").
    - MCP_SERVERS_ENABLED: Optional agent MCPs (default: "playwright"). Supported: playwright, figma.
      FIGMA_ACCESS_TOKEN required for Figma. Playwright runs via npx @playwright/mcp on the backend.
    """
