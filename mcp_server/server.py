"""SpecFlow MCP Server using FastMCP.

Agent harness for automated generation, deployment, and testing of full-stack codebases.
Exposes tools for local specification analysis and planning, document reading, parallel
code generation, status polling, output download, and generation retry.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

# Configure logging early, before importing other modules that may log
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP, Context
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from pydantic import FileUrl

from schemas.generation_workflow_enums import GenerationCheckpoint, GenerationStatus
from services.bundled_skills import SKILLS as _SKILLS
from services.generation_orchestrator import GenerationOrchestrator
from services.specflow_backend import BackendContractRejection, call_backend_endpoint
from services.cli_service import download_and_extract_outputs
from services.server_instructions import SERVER_INSTRUCTIONS
from services.session import (
    apply_project_root_from_context,
    resolve_generation_id,
    resolve_path,
    session_file,
    set_project_root,
    write_session,
)
from services.file_sync import ensure_gain_json
from services.run_generation_precheck import RejectionCode as PrecheckRejectionCode
from services.run_generation_precheck import precheck as run_generation_precheck
from services.validate_models import (
    blocking_rejection as model_blocking_rejection,
    request_model_validation,
    validate_models_on_connect,
)
from services.document_reader import (
    DocumentContent,
    is_image_file,
    is_supported_document,
    read_document as _read_document,
)
from services.tool_helpers import (
    check_status_safe,
    is_generation_in_progress,
    resolve_spec_context,
    resolve_workspace_count,
)
from services.user_response import brief_sentences, chat_json
from services.retry import (
    AlreadyRunning,
    BackendError,
    PendingNotFailed,
    PendingRejectedBeforeCodegen,
    Queued,
    retry_generation_core,
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
logger.info("MCP Server starting with Backend URL: %s", BACKEND_URL)
if "BACKEND_URL" not in os.environ:
    logger.warning("BACKEND_URL not set in environment, using default: %s", BACKEND_URL)

async def _bootstrap_gain_json(ctx: Context) -> None:
    """Create gain.json at the workspace root as soon as the client connects."""
    try:
        roots = await ctx.list_roots()
        if not roots:
            return
        uri = getattr(roots[0], "uri", None)
        if not isinstance(uri, FileUrl):
            return
        project_root = Path(uri.path)
        if project_root.is_dir():
            ensure_gain_json(project_root)
    except Exception as e:
        logger.debug("Could not bootstrap gain.json at startup: %s", e)


class _GainJsonBootstrapMiddleware(Middleware):
    """Triggers gain.json creation and a model-config check right after the client connects."""

    async def on_initialize(self, context: MiddlewareContext, call_next) -> object:
        result = await call_next(context)
        if context.fastmcp_context is not None:
            for coro in (
                _bootstrap_gain_json(context.fastmcp_context),
                validate_models_on_connect(context.fastmcp_context),
            ):
                task = asyncio.create_task(coro)
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        return result


_background_tasks: set[asyncio.Task] = set()

mcp = FastMCP(name="SpecFlow - AI Agent Harness", instructions=SERVER_INSTRUCTIONS)
mcp.add_middleware(_GainJsonBootstrapMiddleware())


_CHECKPOINT_LABELS: dict[str, str] = {
    GenerationCheckpoint.FILES_UPLOADED: "Files received",
    GenerationCheckpoint.CONTRACT_VALIDATED: "Files validated — preparing agents",
    GenerationCheckpoint.KB_INIT_DONE: "Knowledge base initialized",
    GenerationCheckpoint.GENERATION_STARTED: "Generating code",
    GenerationCheckpoint.GENERATION_DONE: "Code generated — deploying",
    GenerationCheckpoint.DEPLOY_AND_E2E_DONE: "Deploy and E2E complete",
    GenerationCheckpoint.OUTPUTS_ARCHIVED: "Outputs archived",
    GenerationCheckpoint.ESTIMATION_DONE: "Estimation complete",
}


def _phase_label(status: str, checkpoint: str | None) -> str:
    """Return a short human-readable phase description for check_status responses."""
    if status == GenerationStatus.INITIALIZING:
        return "Allocating workspaces"
    if status == GenerationStatus.COMPLETED:
        return "Done — outputs ready"
    if status == GenerationStatus.FAILED:
        return "Failed"
    if status == GenerationStatus.RUNNING and checkpoint:
        return _CHECKPOINT_LABELS.get(checkpoint, "Running")
    if status == GenerationStatus.RUNNING:
        return "Running"
    return f"Status: {status}"


def _status_chat_message(response_data: dict) -> str:
    """One or two sentences for check_status chat display."""
    status = (response_data.get("status") or "").lower()
    phase = response_data.get("phase") or _phase_label(
        status, response_data.get("checkpoint")
    )
    if status == GenerationStatus.PENDING:
        err = (response_data.get("error") or "").strip()
        if err:
            return brief_sentences(
                f"Upload was rejected: {err} Fix the files locally, then call `run_generation` again."
            )
        return "Ready for `run_generation` when your local analysis and plan files are in place."
    if status == GenerationStatus.COMPLETED:
        return "Generation finished. Use `download_outputs` if you want the artifacts locally."
    if status == GenerationStatus.FAILED:
        err = (response_data.get("error") or "").strip()
        if err:
            return brief_sentences(
                f"Generation failed: {err} Use `retry_generation` only if coding had already started."
            )
        return "Generation failed. Use `retry_generation` if you want to resume from the last checkpoint."
    if status in (GenerationStatus.RUNNING, GenerationStatus.INITIALIZING):
        return brief_sentences(f"Generation is in progress ({phase}). You'll get an email when it finishes.")
    return brief_sentences(f"Status is {status or 'unknown'} ({phase}).")


def _rejection_chat_payload(rejection_dict: dict) -> str:
    """Precheck / guard rejection: brief message plus structured fields in details."""
    msg = rejection_dict.get("error") or rejection_dict.get("message") or "Request rejected."
    return chat_json(
        msg,
        details=rejection_dict,
        code=rejection_dict.get("code"),
        missing_files=rejection_dict.get("missing_files"),
        ambiguous=rejection_dict.get("ambiguous"),
        generation_id=rejection_dict.get("generation_id"),
    )


def _make_prompt_text(skill_name: str, **substitutions: str) -> str:
    """Return the SKILL.md content for the given skill, with placeholder substitution.

    Placeholders in the SKILL.md content use the form `<<KEY>>` and are replaced with
    the corresponding value from `substitutions`. Missing placeholders are left as-is
    so the LLM can still see them and ask the user for clarification rather than
    silently using a stale value.

    Raises:
        KeyError: If the skill is not bundled — fail fast rather than return empty text.
    """
    skill = next((s for s in _SKILLS if s["name"] == skill_name), None)
    if skill is None:
        raise KeyError(f"Skill '{skill_name}' is not bundled — check services/skills/")
    content = skill["content"]
    for key, value in substitutions.items():
        content = content.replace(f"<<{key.upper()}>>", value)
    return content


def _local_skill_tool_response(skill_name: str, *, writes_to: str, **substitutions: str) -> str:
    """Build the JSON payload for a local-only SpecFlow skill tool."""
    return json.dumps(
        {
            "mode": "local",
            "skill": skill_name,
            "spec_dir": substitutions.get("spec_dir", "specs"),
            "outputs_dir": substitutions.get("outputs_dir", "docs"),
            "src_dir": substitutions.get("src_dir", "src"),
            "writes_to": writes_to,
            "template": _make_prompt_text(skill_name, **substitutions),
            "message": (
                "Follow the template in your IDE — no backend upload or session is created. "
                "Repeat anytime specs or plans change, then call `run_generation` when ready."
            ),
        },
        indent=2,
    )


@mcp.tool()
def check_specification_completeness(
    spec_dir: str = "specs",
    outputs_dir: str = "docs",
    src_dir: str = "src",
) -> str:
    """
    Analyze specification completeness locally.

    Returns only the
    agent instruction template (bundled `specflow-analysis` SKILL.md). Your IDE agent
    reads `spec_dir`, inspects optional brownfield code under `src_dir`, and writes
    `{outputs_dir}/analysis/specification_completeness.md`. No backend sync, API key,
    workspace, or generation session. Safe to call repeatedly; does NOT start generation.

    Args:
        spec_dir: Specification directory relative to the project root (default: "specs").
        outputs_dir: Root for analysis/planning artifacts (default: "docs"). Must match
                     `run_generation` and `run_planning`.
        src_dir: Existing source tree for brownfield context (default: "src").

    Returns:
        JSON with `template` (full instructions), `writes_to`, and path arguments.
    """
    return _local_skill_tool_response(
        "specflow-analysis",
        writes_to=f"{outputs_dir}/analysis/specification_completeness.md",
        spec_dir=spec_dir,
        outputs_dir=outputs_dir,
        src_dir=src_dir,
    )


@mcp.tool()
def run_planning(
    spec_dir: str = "specs",
    outputs_dir: str = "docs",
    src_dir: str = "src",
) -> str:
    """
    Create a phased implementation plan locally.

    Returns only the
    agent instruction template (bundled `specflow-planning` SKILL.md). Your IDE agent uses
    specs plus `{outputs_dir}/analysis/specification_completeness.md` and writes
    `{outputs_dir}/planning/IMPLEMENTATION_PLAN.md` (and `e2e-test-plan.md` when Part F is
    INTEGRATION_TESTS_READY). Repeatable before `run_generation`; no backend call.

    Args:
        spec_dir: Specification directory (default: "specs"). Same as
                  `check_specification_completeness`.
        outputs_dir: Outputs root (default: "docs"). Same as `check_specification_completeness`.
        src_dir: Optional existing code for brownfield planning (default: "src").

    Returns:
        JSON with `template` (full instructions), `writes_to`, and path arguments.
    """
    writes_to = (
        f"{outputs_dir}/planning/IMPLEMENTATION_PLAN.md"
        f" (and optionally {outputs_dir}/planning/e2e-test-plan.md)"
    )
    return _local_skill_tool_response(
        "specflow-planning",
        writes_to=writes_to,
        spec_dir=spec_dir,
        outputs_dir=outputs_dir,
        src_dir=src_dir,
    )


@mcp.prompt(
    name="specflow-diagnose",
    description="Diagnose errors and symptoms from a SpecFlow-deployed app.",
)
def prompt_specflow_diagnose() -> str:
    return _make_prompt_text("specflow-diagnose")


@mcp.prompt(
    name="specflow-compare-variants",
    description="Compare and assemble code from 1–3 SpecFlow workspace repos.",
)
def prompt_specflow_compare_variants() -> str:
    return _make_prompt_text("specflow-compare-variants")


@mcp.tool()
async def read_document(
    file_path: str,
    *,
    ctx: Context,
) -> str:
    """Extract text and images from PDF, DOCX, PPTX, XLSX, or CSV files.

    Returns clean markdown with structure preserved (headings, tables, lists,
    speaker notes).  Embedded images from PDF and PPTX are extracted and
    returned as base64 so the IDE's vision model can interpret them.

    For standalone image files (.png, .jpg, …), use the IDE's built-in file
    reader instead — this tool handles document formats that IDEs typically
    cannot read natively.

    Supported: .pdf, .docx, .pptx, .xlsx, .xls, .csv

    Args:
        file_path: Path to the document (absolute, or relative to project root).

    Returns:
        JSON with markdown content, embedded images (base64), and any warnings.
    """
    await apply_project_root_from_context(ctx)
    path = resolve_path(file_path)

    if is_image_file(path):
        return json.dumps({
            "error": (
                f"{path.name} is an image file. "
                "Use the IDE's built-in file reader or vision tool to view it directly."
            ),
        }, indent=2)

    if not is_supported_document(path):
        return json.dumps({
            "error": (
                f"Unsupported format: {path.suffix}. "
                "Supported: .pdf, .docx, .pptx, .xlsx, .xls, .csv"
            ),
        }, indent=2)

    try:
        result: DocumentContent = _read_document(path)
    except (FileNotFoundError, ValueError) as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    except Exception as exc:
        logger.error("read_document failed for %s: %s", path, exc, exc_info=True)
        return json.dumps({
            "error": f"Failed to read {path.name}: {exc}",
        }, indent=2)

    response: dict = {
        "file": str(path),
        "format": path.suffix.lower(),
        "markdown": result.markdown,
    }
    if result.page_count is not None:
        response["page_count"] = result.page_count
    if result.images:
        response["images"] = [
            {
                "label": img.label,
                "mime_type": img.mime_type,
                "data_base64": img.data_b64,
                **({"width": img.width} if img.width else {}),
                **({"height": img.height} if img.height else {}),
            }
            for img in result.images
        ]
        response["image_count"] = len(result.images)
        response["image_note"] = (
            "Embedded images are included as base64. "
            "If your IDE supports vision, it can interpret them directly. "
            "If not, the text content above should still be sufficient for analysis."
        )
    if result.warnings:
        response["warnings"] = result.warnings

    return json.dumps(response, indent=2)


@mcp.tool()
async def run_generation(
    spec_dir: str = "specs",
    outputs_dir: str = "docs",
    src_dir: str = "src",
    generation_id: str | None = None,
    *,
    ctx: Context,
) -> str:
    """
    Start code generation. Runs autonomously for 2–8 hours.

    Prerequisites:
      - `check_specification_completeness` produced `{outputs_dir}/analysis/specification_completeness.md`
      - `run_planning` produced `{outputs_dir}/planning/IMPLEMENTATION_PLAN.md`
        (and `e2e-test-plan.md` if analysis says INTEGRATION_TESTS_READY)

    If any required file is missing, this tool refuses immediately with a message naming
    the missing file and which local tool to re-run. No upload until validation passes.

    ⚠️ DO NOT AUTO-RETRY on any error — the workflow may have started. Ask the user what to do.

    Args:
        spec_dir: Spec directory. Must match `check_specification_completeness` and
                  `run_planning`. Default: "specs".
        outputs_dir: Outputs directory. Must match the value used with the local skills.
                     Default: "docs".
        src_dir: Existing source tree for brownfield context (default: "src").
        generation_id: Optional override; otherwise loaded from `specflow_session.json`
                       when present. Reuse continues a session already allocated on the
                       backend (e.g. after contract rejection on a prior upload).

    Returns:
        JSON with generation_id, status, and a short message.
    """
    try:
        spec_dir_path, project_root, src_dir_path, error = await resolve_spec_context(spec_dir, src_dir, ctx)
        if error:
            return error

        rejection = run_generation_precheck(project_root, spec_dir, outputs_dir)
        if rejection is not None:
            logger.info("run_generation precheck rejected: %s", rejection.code.value)
            return _rejection_chat_payload(rejection.to_dict())

        generation_id = resolve_generation_id(generation_id, project_root)
        if generation_id:
            status_data = await check_status_safe(generation_id)
            if is_generation_in_progress(status_data):
                return _rejection_chat_payload({
                    "error": (
                        "A generation is already running. "
                        "Wait for the email notification before starting another one."
                    ),
                    "code": PrecheckRejectionCode.GENERATION_ALREADY_RUNNING,
                    "generation_id": generation_id,
                })

        # Model pre-flight: refuse before any upload if a configured model is unavailable
        # on the active provider (block-on-any-invalid). Permissive if the check itself
        # can't run — the upload below would surface a backend-unreachable error anyway.
        try:
            model_validation = await request_model_validation()
            rejection = model_blocking_rejection(model_validation)
            if rejection is not None:
                logger.info("run_generation rejected: MODEL_UNAVAILABLE")
                return _rejection_chat_payload({**rejection, "generation_id": generation_id})
        except Exception as e:
            logger.warning("Model validation pre-flight skipped: %s", e)

        response_data = await GenerationOrchestrator.run_generation(
            spec_dir=spec_dir_path,
            src_dir=src_dir_path,
            outputs_dir=outputs_dir,
            generation_id=generation_id,
            workspace_count=resolve_workspace_count(),
        )
        new_id = response_data.get("generation_id")
        if new_id:
            write_session(new_id, project_root)

        return chat_json(
            "Files uploaded and generation started. You'll get an email when it finishes (usually 2–8 hours).",
            details=response_data,
            generation_id=new_id,
            status=response_data.get("status", GenerationStatus.PENDING),
        )

    except BackendContractRejection as rej:
        # Backend-side rejection (e.g. PLAN_NO_PHASES caught by the pre-allocation
        # preflight). Surface it with the same shape as an MCP-side precheck so the
        # user gets the actionable message + code, not a generic "server unreachable".
        logger.info("run_generation backend-rejected: %s", rej.detail.get("code"))
        return _rejection_chat_payload(rej.detail)
    except Exception as e:
        logger.error("Generation run failed: %s", e, exc_info=True)
        return chat_json(
            "Couldn't start generation. Check the SpecFlow server is reachable and try again.",
            details={"error": str(e)},
        )


@mcp.tool()
async def check_status(
    generation_id: str | None = None,
    spec_dir: str | None = None,
    *,
    ctx: Context,
) -> str:
    """
    Check progress of a running generation.

    Read-only. Only call when the user explicitly asks for an update — never on a loop.

    Args:
        generation_id: Optional; defaults to the session file.
        spec_dir: Optional absolute spec directory. Only needed after an MCP restart if
                  no session file exists yet — provides the project root.

    Returns:
        JSON with status, phase, and a short message.
    """
    await apply_project_root_from_context(ctx)
    if spec_dir:
        sp = Path(spec_dir)
        if sp.is_absolute():
            set_project_root(sp.parent)

    try:
        generation_id = resolve_generation_id(generation_id)
        if not generation_id:
            return chat_json(
                "No active generation in this project. Run `run_generation` to start one.",
            )

        response_text = await call_backend_endpoint(
            endpoint=f"/api/v1/generation-sessions/{generation_id}/status",
            method="GET",
            timeout_seconds=30,
        )
        response_data = json.loads(response_text)

        status = response_data.get("status", "").lower()
        checkpoint = response_data.get("checkpoint", "")

        response_data["can_run_generation"] = status == GenerationStatus.PENDING
        response_data["phase"] = _phase_label(status, checkpoint)

        try:
            sf = session_file()
            response_data["session_file"] = str(sf)
            if status in (GenerationStatus.COMPLETED, GenerationStatus.FAILED):
                response_data["session_note"] = f"Done. Delete {sf} to start a new session."
        except RuntimeError:
            pass

        return chat_json(
            _status_chat_message(response_data),
            details=response_data,
            generation_id=generation_id,
            status=status,
            checkpoint=checkpoint,
            phase=response_data.get("phase"),
        )

    except Exception as e:
        return chat_json(
            "Couldn't reach the SpecFlow server. Try again in a moment.",
            details={"error": str(e)},
        )


@mcp.tool()
async def download_outputs(
    generation_id: str,
    outputs_dir: str = "docs",
    *,
    ctx: Context,
) -> str:
    """
    Download and extract the archived outputs of a completed generation cycle.

    ⛔ NEVER call automatically — only call on explicit user request (e.g. "download the outputs").

    Works even after workspaces are wiped — outputs are stored on the artifact path.

    Archive layout inside the extracted directory:
      {generation_id}/
        {workspace_id}/   workspace snapshot (source code + outputs)
        analysis/         spec analysis outputs (if analysis was run)
        report/           combined multi-workspace comparison report

    Args:
        generation_id: ID of the generation to download outputs for.
        outputs_dir: Local directory to extract files into (default: "docs").
                     Created if it does not exist.

    Returns:
        JSON with extraction summary: files_extracted, outputs_dir, file list.
    """
    try:
        await apply_project_root_from_context(ctx)
        # SSOT for download + path-traversal-safe extraction lives in cli_service,
        # shared with the CLI (DRY). Returns the parsed no-outputs JSON dict or the
        # extraction summary dict.
        result = await download_and_extract_outputs(generation_id, outputs_dir)
        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({
            "error": "Couldn't download outputs. Generation may still be running or the server is unreachable.",
            "details": str(e),
        }, indent=2)


@mcp.tool()
async def retry_generation(
    generation_id: str | None = None,
    spec_dir: str | None = None,
    *,
    ctx: Context,
) -> str:
    """
    Retry a failed generation. Reuses the same workspaces.

    Only call on explicit user request after a generation has failed.

    Args:
        generation_id: Optional; defaults to the session file.
        spec_dir: Optional absolute spec directory — only needed after an MCP restart.

    Returns:
        JSON with retry status and a short message.
    """
    await apply_project_root_from_context(ctx)
    if spec_dir:
        sp = Path(spec_dir)
        if sp.is_absolute():
            set_project_root(sp.parent)

    generation_id = resolve_generation_id(generation_id)
    if not generation_id:
        return chat_json(
            "No previous generation found. Run `run_generation` to start one.",
        )

    match await retry_generation_core(generation_id):
        case AlreadyRunning():
            return _rejection_chat_payload({
                "error": "A generation is already running. Wait for it to finish before retrying.",
                "code": PrecheckRejectionCode.GENERATION_ALREADY_RUNNING,
                "generation_id": generation_id,
            })
        case PendingRejectedBeforeCodegen(status_data=status_data, error=error):
            return chat_json(
                brief_sentences(
                    f"Last run was rejected before codegen: {error} "
                    "Fix files locally and call `run_generation` — not `retry_generation`."
                ),
                details=status_data,
                generation_id=generation_id,
            )
        case PendingNotFailed(status_data=status_data):
            return chat_json(
                "Session is pending but has not failed. Use `run_generation`, not `retry_generation`.",
                details=status_data,
                generation_id=generation_id,
            )
        case Queued(backend_data=backend_data):
            return chat_json(
                "Retry queued. Generation will resume from the last checkpoint on the same workspaces.",
                details=backend_data,
                generation_id=generation_id,
                status=backend_data.get("status"),
                retry_count=backend_data.get("retry_count"),
            )
        case BackendError(error=error):
            return chat_json(
                "Couldn't retry. The SpecFlow server may be unreachable.",
                details={"error": error},
            )


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
