# gd-specflow

SpecFlow MCP server and CLI — spec-driven AI code generation from your IDE.

SpecFlow is an agent harness that automates specification analysis, implementation planning, and large-scale parallel code generation. This package provides the MCP server (`specflow-mcp`) and the terminal UI (`specflow tui`).

Requires **Python 3.13**, **Docker**, and **`uv`** ([install](https://github.com/astral-sh/uv)).

## Local agentic harness sandbox setup

Codegen runs against a **local Docker backend** in the main SpecFlow repository — installing this package alone is not enough.

Entry point: **[README.md](https://github.com/griddynamics/specflow/blob/main/README.md)** 

Short instructions:

1. Clone the repo and enter it:

   ```bash
   git clone https://github.com/griddynamics/specflow.git
   cd specflow
   ```

2. Install Specflow (includes the Terminal UI that guides you through onboarding)
   ```bash
   uv tool install --editable "./mcp_server"
   ```

4. Bootstrap the sandbox - interactive wizard

   ```bash
   specflow tui
   ```


## MCP tools

| Tool | Description |
| --- | --- |
| `check_specification_completeness` | Analyze specs for gaps and contradictions (local) |
| `run_planning` | Generate a phased implementation plan (local) |
| `read_document` | Extract PDF/DOCX/PPTX/XLSX/CSV to markdown (local) |
| `run_generation` | Upload and launch parallel codegen on the backend |
| `check_status` | Poll generation progress |
| `download_outputs` | Download artifacts from a completed run |
| `retry_generation` | Retry a failed generation |

## Documentation

- [Repository & overview](https://github.com/griddynamics/specflow)
- [Local quickstart](https://github.com/griddynamics/specflow/blob/main/QUICKSTART.md)
- [MCP setup guide](https://github.com/griddynamics/specflow/blob/main/MCP_USER.md)
- [MCP API reference](https://github.com/griddynamics/specflow/blob/main/docs/mcp/API_REFERENCE.md)

## License

MIT — Copyright (c) 2024 Grid Dynamics International, Inc.
