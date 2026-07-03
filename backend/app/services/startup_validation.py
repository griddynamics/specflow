"""
Startup Validation Service

Validates system state on instance startup.
All checks must be idempotent and safe to run concurrently from multiple instances.

Critical Checks:
- Environment variables are set
- Firestore is reachable
- Workspace pool has available workspaces
- Filestore mount is accessible

Used by:
- FastAPI lifespan handler (on_event("startup"))
- Health check endpoints (/health, /health/ready)
"""

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from app.core.config import is_key_valid, settings
from app.core.enums import DatabaseType, LLMProvider
from app.database.interface import IDatabase
from app.services.workspace_pool import WorkspacePoolService

logger = logging.getLogger("api.main")


class StartupValidationError(Exception):
    """Raised when critical startup validation fails."""
    pass


class StartupValidator:
    """
    Validates system state on instance startup.
    
    All checks are idempotent and safe to run concurrently.
    Critical checks will fail startup if they don't pass.
    """
    
    def __init__(
        self,
        db: IDatabase,
        workspace_pool: WorkspacePoolService,
    ):
        """
        Initialize startup validator.

        Args:
            db: Database interface
            workspace_pool: Workspace pool service
        """
        self._db = db
        self.workspace_pool = workspace_pool
    
    async def run_all_checks(self) -> Dict[str, Any]:
        """
        Run all startup validation checks.
        
        Returns:
            Dictionary of check results
            
        Raises:
            StartupValidationError: If critical check fails
            
        Example:
            >>> validator = StartupValidator(db, workspace_pool)
            >>> results = await validator.run_all_checks()
            >>> print(f"All checks passed: {results}")
        """
        results = {}
        
        # 1. Environment validation (CRITICAL)
        results["environment"] = await self._check_environment()
        if not results["environment"]["passed"]:
            raise StartupValidationError(
                f"Environment check failed: {results['environment']['error']}"
            )
        
        # 2. Firestore connectivity (CRITICAL)
        results["firestore"] = await self._check_firestore_connectivity()
        if not results["firestore"]["passed"]:
            raise StartupValidationError(
                f"Firestore check failed: {results['firestore']['error']}"
            )
        
        # 3. Workspace pool validation (CRITICAL in production, WARNING in test/dev)
        results["workspace_pool"] = await self._check_workspace_pool()
        database_type = os.getenv("DATABASE_TYPE", "firestore")
        
        if not results["workspace_pool"]["passed"]:
            # Non-critical in test/dev environments (emulator, memory)
            if database_type in ["emulator", "memory"]:
                logger.warning(
                    f"Workspace pool check: {results['workspace_pool']['error']} "
                    "(non-critical in test/dev mode)"
                )
            else:
                # Critical in production
                raise StartupValidationError(
                    f"Workspace pool check failed: {results['workspace_pool']['error']}"
                )
        
        # 4. Filestore mount check (CRITICAL in production, WARNING in test/dev)
        results["filestore"] = await self._check_filestore_mount()
        
        if not results["filestore"]["passed"]:
            # Non-critical in test/dev environments
            if database_type in ["emulator", "memory"]:
                logger.warning(
                    f"Filestore check: {results['filestore']['error']} "
                    "(non-critical in test/dev mode)"
                )
            else:
                # Critical in production
                raise StartupValidationError(
                    f"Filestore check failed: {results['filestore']['error']}"
                )
        
        return results
    
    async def _check_environment(self) -> Dict[str, Any]:
        """
        Validate required environment variables are set.

        - Active LLM provider key: required based on settings.DEFAULT_PROVIDER.
        - Git platform (Fernet key + default PAT + git user): required for firestore/emulator
          deployments unless platform secrets are loaded from Kubernetes at startup.
        """
        missing: list[str] = []

        # The provider is derived from whichever key is present (see
        # Settings.DEFAULT_PROVIDER computed_field), so a provider/key mismatch is
        # impossible: the anthropic branch always has its key. The only way the
        # openrouter branch lacks a key is when NEITHER key was set — fail fast
        # naming both so the user knows what to add.
        # is_key_valid so a blank / whitespace-only env value counts as unset,
        # matching how DEFAULT_PROVIDER derives the provider.
        if settings.DEFAULT_PROVIDER == LLMProvider.OPENROUTER:
            if not is_key_valid(os.environ.get("OPENROUTER_API_KEY")):
                missing.append(
                    "OPENROUTER_API_KEY or ANTHROPIC_API_KEY (set one LLM provider key)"
                )
        elif settings.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC:
            if not is_key_valid(os.environ.get("ANTHROPIC_API_KEY")):
                missing.append(
                    "ANTHROPIC_API_KEY (required for the active LLM provider: anthropic)"
                )
        else:
            # Fail closed: a provider with no key-requirement mapping is a misconfiguration.
            missing.append(
                f"API key for the active LLM provider '{settings.DEFAULT_PROVIDER}' "
                "(no key-requirement mapping defined for this provider)"
            )

        raw_db_type = os.environ.get("DATABASE_TYPE", "memory")
        try:
            database_type = DatabaseType(raw_db_type)
        except ValueError:
            database_type = DatabaseType.MEMORY

        if database_type in (DatabaseType.FIRESTORE, DatabaseType.EMULATOR):
            in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))

            k8s_ready = (
                in_cluster
                and bool(settings.K8S_SECRET_NAME)
                and bool(settings.K8S_SECRET_NAMESPACE)
            )
            if not k8s_ready:
                if not os.environ.get("TOKEN_ENCRYPTION_KEY"):
                    missing.append(
                        "TOKEN_ENCRYPTION_KEY (required to encrypt GitHub tokens at rest"
                        " for emulator/firestore mode)"
                    )
                if not (
                    os.environ.get("GITHUB_TOKEN_DEFAULT") or os.environ.get("GITHUB_TOKEN")
                ):
                    missing.append(
                        "GITHUB_TOKEN_DEFAULT (required as the default GitHub PAT for"
                        " workspace repo cloning; legacy alias: GITHUB_TOKEN)"
                    )
                if not (
                    os.environ.get("GIT_USER_NAME_DEFAULT") or os.environ.get("GIT_USER_NAME")
                ):
                    missing.append(
                        "GIT_USER_NAME_DEFAULT (required as the Git committer identity for"
                        " workspace operations; legacy alias: GIT_USER_NAME)"
                    )

        if missing:
            return {
                "passed": False,
                "error": f"Missing environment variables: {', '.join(missing)}",
            }

        return {"passed": True, "error": None}
    
    async def _check_firestore_connectivity(self) -> Dict[str, Any]:
        """
        Verify Firestore is reachable and responsive.
        
        Uses a simple read operation to validate connectivity.
        Includes retry logic with exponential backoff for emulator startup.
        """
        import os
        
        # Log diagnostic information
        emulator_host = os.getenv("FIRESTORE_EMULATOR_HOST")
        database_type = os.getenv("DATABASE_TYPE", "memory")
        
        logger.info(
            f"Firestore connectivity check - "
            f"DATABASE_TYPE={database_type}, "
            f"FIRESTORE_EMULATOR_HOST={emulator_host}"
        )
        
        if database_type == "emulator" and not emulator_host:
            return {
                "passed": False,
                "error": "DATABASE_TYPE=emulator but FIRESTORE_EMULATOR_HOST is not set"
            }
        
        # For emulator mode, verify host:port is reachable first
        if database_type == "emulator" and emulator_host:
            try:
                host, port = emulator_host.split(":")
                port = int(port)
                
                # Try to connect to the host:port to verify it's reachable
                logger.info(f"Checking if emulator host is reachable: {host}:{port}")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)  # 2 second timeout
                result = sock.connect_ex((host, port))
                sock.close()
                
                if result != 0:
                    return {
                        "passed": False,
                        "error": f"Cannot reach Firestore emulator at {emulator_host} "
                                f"(connection refused). Is the emulator container running?"
                    }
                
                logger.info(f"Emulator host {emulator_host} is reachable")
            except (ValueError, socket.gaierror) as e:
                return {
                    "passed": False,
                    "error": f"Invalid FIRESTORE_EMULATOR_HOST format '{emulator_host}': {str(e)}"
                }
            except Exception as e:
                logger.warning(f"Could not verify emulator host reachability: {e}, proceeding anyway")
        
        # Retry logic for emulator startup (common issue)
        max_retries = 5
        retry_delay = 2  # Start with 2 seconds
        
        for attempt in range(max_retries):
            try:
                # Try to query a collection (doesn't need to exist)
                # This validates connectivity and authentication
                # Wrap in to_thread to avoid blocking the event loop
                # Use asyncio.wait_for to add a timeout
                _ = await asyncio.wait_for(
                    asyncio.to_thread(self._db.query, "_health_check", []),
                    timeout=10.0  # 10 second timeout per attempt
                )
                
                logger.info(f"Firestore connectivity check passed on attempt {attempt + 1}")
                return {"passed": True, "error": None}
            
            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Firestore connectivity check timed out (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    return {
                        "passed": False,
                        "error": f"Firestore connection timeout after {max_retries} attempts. "
                                f"Check if emulator is running at {emulator_host}"
                    }
            
            except Exception as e:
                error_msg = str(e)
                
                # Check if it's a connection error that might be transient
                is_connection_error = any(
                    keyword in error_msg.lower() 
                    for keyword in ["connection refused", "failed to connect", "timeout", "unavailable"]
                )
                
                if is_connection_error and attempt < max_retries - 1:
                    logger.warning(
                        f"Firestore connection error (attempt {attempt + 1}/{max_retries}): {error_msg}, "
                        f"retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    return {
                        "passed": False,
                        "error": f"Firestore connection failed: {error_msg}"
                    }
        
        # Should never reach here, but just in case
        return {
            "passed": False,
            "error": "Firestore connectivity check failed after all retries"
        }
    
    async def _check_workspace_pool(self) -> Dict[str, Any]:
        """
        Validate workspace pool exists and has available workspaces.
        
        Checks:
        - Workspaces collection exists and has documents
        - At least one workspace set is available
        """
        try:
            pool_status = await self.workspace_pool.get_pool_status()

            if not pool_status.get("total"):
                return {
                    "passed": False,
                    "error": "Workspace pool is empty. Run initialization script."
                }
            available_sets = pool_status.get("available_sets", 0)
            
            return {
                "passed": True,
                "error": None,
                "available_sets": available_sets,
                "total_workspaces": pool_status.get("total", 0),
                "warning": "Low workspace availability" if available_sets < 2 else None
            }
        
        except Exception as e:
            return {
                "passed": False,
                "error": f"Workspace pool check failed: {str(e)}"
            }
    
    async def _check_filestore_mount(self) -> Dict[str, Any]:
        """
        Verify Filestore NFS mount is accessible.
        
        Checks:
        - Directory exists
        - We can write to it
        """
        workspace_base = os.getenv("WORKSPACE_BASE_PATH", "/workspaces")
        
        try:
            workspace_path = Path(workspace_base)
            
            # Check directory exists
            if not workspace_path.exists():
                return {
                    "passed": False,
                    "error": f"Workspace directory not found: {workspace_base}"
                }
            
            if not workspace_path.is_dir():
                return {
                    "passed": False,
                    "error": f"Workspace path is not a directory: {workspace_base}"
                }
            
            # Check we can write (create temp file)
            test_file = workspace_path / ".health_check"
            try:
                test_file.write_text("ok")
                test_file.unlink()
            except PermissionError:
                return {
                    "passed": False,
                    "error": f"Cannot write to workspace directory: {workspace_base}"
                }
            
            return {"passed": True, "error": None, "path": str(workspace_path)}
        
        except Exception as e:
            return {
                "passed": False,
                "error": f"Filestore check failed: {str(e)}"
            }
    


# Global state - tracks if startup completed successfully
startup_state = {
    "complete": False,
    "healthy": False,
    "error": None,
    "checks": {},
    "started_at": None,
    "completed_at": None,
}


def get_startup_state() -> Dict[str, Any]:
    """Get current startup state."""
    return startup_state.copy()


def mark_startup_started() -> None:
    """Mark startup validation as started."""
    startup_state["started_at"] = datetime.now(timezone.utc)
    startup_state["complete"] = False
    startup_state["healthy"] = False


def mark_startup_complete(
    healthy: bool,
    checks: Dict[str, Any],
    error: Optional[str] = None
) -> None:
    """
    Mark startup validation as complete.
    
    Args:
        healthy: Whether startup validation passed
        checks: Dictionary of check results
        error: Error message if startup failed
    """
    startup_state["complete"] = True
    startup_state["healthy"] = healthy
    startup_state["checks"] = checks
    startup_state["error"] = error
    startup_state["completed_at"] = datetime.now(timezone.utc)
