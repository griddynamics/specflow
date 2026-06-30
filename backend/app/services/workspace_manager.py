"""
Workspace manager for multi-provider support.

Each workspace represents a repository/project that can use
a different AI provider and model configuration.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Final, List, Optional, Tuple

from app.utils.workspace_gitignore import ensure_workspace_gitignore, read_gitignore_patterns
from app.services.git_utils import GitCommandError, run_git

from app.core.config import DEFAULT_MODEL, Settings, WORKSPACE_DEFAULT_BRANCH, settings as global_settings
from app.core.rosetta_kb import RosettaKbMode, resolve_rosetta_kb_mode, rosetta_plugin_root
from app.schemas.workspace import WorkspaceSettings
from app.services.providers import BaseProvider, ProviderFactory
from app.services.providers.credentials import (
    resolve_provider_api_key,
    resolve_provider_base_url,
)

# KB init writes under rosetta/<name>/ (not `.claude/`) to avoid the SDK sensitive-path guard.
# unpack_rosetta_artifacts maps these into `.claude/<name>/` for Claude Code discovery.
_ROSETTA_DOT_CLAUDE_SUBDIRS: Final[frozenset[str]] = frozenset(
    {"agents", "skills", "commands"}
)

# Conventional fallback (plugin source dir, workspace `.claude/` destination) pairs, used when
# the plugin manifest can't be read. The real mapping is resolved per-plugin from plugin.json by
# `_resolve_plugin_claude_map` (the Rosetta plugin declares "commands": "./workflows/", so
# workflows -> commands). Ordered pairs (not a dict keyed on source) so two sources that happen
# to resolve to the same name can't silently collide and drop a tree. Only these discovery-shaped
# trees are copied into the workspace; the plugin's other files (hook scripts, rules/, templates/)
# stay in the read-only image at settings.ROSETTA_PLUGIN_PATH, which CLAUDE_PLUGIN_ROOT points at
# (see claude_code.setup_rosetta_plugin_env).
_ROSETTA_PLUGIN_CLAUDE_MAP: Final[Tuple[Tuple[str, str], ...]] = (
    ("agents", "agents"),
    ("skills", "skills"),
    ("workflows", "commands"),
)

# Project-level instructions file the KB-init agent produces at the workspace root. Propagated
# from the primary to each extra workspace (in provisioned-plugin mode the agent writes it there
# directly; in MCP mode unpack also recreates it per-workspace, so re-syncing it is harmless).
_ROSETTA_ROOT_CLAUDE_MD: Final[str] = "CLAUDE.md"

# Names that must NEVER be copied between workspaces or removed when clearing the workspace
# root — losing a workspace's own ``.git`` destroys its ``origin`` / per-workspace remote
# (Steel Commandments I & III). This invariant is enforced independently of the configurable
# EXCLUDED_ARTIFACT_PATTERNS, which an operator could override and accidentally omit ``.git``.
_ALWAYS_PRESERVE_NAMES: Final[frozenset[str]] = frozenset({".git"})


class WorkspaceManager:
    """
    Manages workspace configurations and provider instances.
    
    Provides a central point for:
    - Creating and managing workspace settings
    - Instantiating providers for workspaces
    - Caching provider instances
    """
    
    def __init__(self, settings: Settings, logger: Optional[logging.Logger] = None):
        """
        Initialize workspace manager.
        
        Args:
            settings: Global settings instance
            logger: Optional logger instance
        """
        self.settings = settings
        self.logger = logger or logging.getLogger(__name__)
        self._provider_cache: Dict[str, BaseProvider] = {}
    
    def create_workspace_settings(
        self,
        workspace_path: str | Path,
        provider: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        name: Optional[str] = None,
        provider_config: Optional[Dict] = None,
        **kwargs
    ) -> WorkspaceSettings:
        """
        Create workspace settings.
        
        Args:
            workspace_path: Path to the workspace (relative to base path)
            provider: Provider name ("anthropic", "openrouter", etc.)
            model: Model name (provider-specific)
            name: Optional workspace name (generated if not provided)
            provider_config: Provider-specific configuration
            **kwargs: Additional workspace settings
            
        Returns:
            WorkspaceSettings instance
        """
        # Resolve provider at call-time so env-override via settings stays live
        resolved_provider: str = provider or global_settings.DEFAULT_PROVIDER

        # Generate name if not provided
        if name is None:
            name = Path(workspace_path).name or str(workspace_path)

        return WorkspaceSettings(
            name=name,
            workspace_path=workspace_path,
            provider=resolved_provider,
            model=model,
            base_path=self.settings.AGENT_BASE_PATH,
            provider_config=provider_config or {},
            **kwargs
        )
    
    def get_provider(
        self,
        workspace_settings: WorkspaceSettings,
        cache_key: Optional[str] = None
    ) -> BaseProvider:
        """
        Get or create a provider instance for a workspace.
        
        Args:
            workspace_settings: Workspace configuration
            cache_key: Optional cache key (defaults to provider:model combination)
            
        Returns:
            Provider instance
        """
        # Create unique cache key that includes model to avoid sharing providers
        # across workspaces with different models
        if cache_key is None:
            cache_key = f"{workspace_settings.provider}:{workspace_settings.model}"
        
        # Check cache
        if cache_key in self._provider_cache:
            self.logger.debug(f"WS name: {workspace_settings.name}: Using cached provider: {cache_key}")
            return self._provider_cache[cache_key]
        
        # Get API key based on provider
        api_key = self._get_api_key_for_provider(workspace_settings.provider)
        
        # Get base URL (provider-specific)
        base_url = self._get_base_url_for_provider(workspace_settings.provider)
        
        # Get provider config, ensuring it's a dict (handles Mock objects in tests)
        provider_config = workspace_settings.provider_config
        if not isinstance(provider_config, dict):
            provider_config = {}
        
        # Create provider with workspace-specific config
        provider = ProviderFactory.create_provider(
            provider_name=workspace_settings.provider,
            api_key=api_key,
            base_url=base_url,
            logger=self.logger,
            **provider_config
        )

        # Model availability is validated against the live provider catalog by
        # app.services.model_validation (run_generation gate); no per-provider check here.

        # Cache and return
        self._provider_cache[cache_key] = provider
        return provider
    
    def _get_api_key_for_provider(self, provider: str) -> str:
        """
        Get API key for a provider from settings.
        
        Args:
            provider: Provider name
            
        Returns:
            API key
            
        Raises:
            ValueError: If API key is not configured
        """
        api_key = resolve_provider_api_key(provider, self.settings)

        if not api_key:
            raise ValueError(
                f"No API key configured for provider: {provider}. "
                f"Please set the appropriate environment variable."
            )
        
        return api_key
    
    def _get_base_url_for_provider(self, provider: str) -> Optional[str]:
        """
        Get base URL for a provider from settings (if configured).
        
        Args:
            provider: Provider name
            
        Returns:
            Base URL from settings or None to let provider use its default
        """
        # Provider-specific base URL (pattern: PROVIDER_BASE_URL, e.g. OPENROUTER_BASE_URL).
        # Returns None when unset, letting the provider use its built-in default.
        return resolve_provider_base_url(provider, self.settings)
    
    def clear_cache(self):
        """Clear the provider cache."""
        self._provider_cache.clear()
        self.logger.info("Provider cache cleared")
    
    def list_cached_providers(self) -> list[str]:
        """Get list of cached provider keys."""
        return list(self._provider_cache.keys())
    
    # Workspace Preparation Methods for Isolated Workspace Model
    
    def prepare_single_workspace(
        self,
        workspace: WorkspaceSettings,
        spec_path: str,
        outputs_dir: str,
        standards_source: Optional[str] = None
    ) -> None:
        """
        Prepare a single workspace for agent execution.
        
        This is used for single-workspace workflows (e.g., spec indexing, completeness check).
        Currently a no-op as the workspace is assumed to be pre-populated, but we copy
        standards files if needed.
        
        Args:
            workspace: Workspace to prepare
            spec_path: Relative path to specifications (e.g., "specifications")
            outputs_dir: Relative path to outputs (e.g., "specflow")
            standards_source: Optional source path for standards files (defaults to /agent/standards)
        """
        self.logger.info(f"Preparing single workspace: {workspace.get_isolated_root()}")

        ensure_workspace_gitignore(Path(workspace.get_isolated_root()))
        
        # Copy standards files to workspace if source provided
        if standards_source:
            self.copy_standards_to_workspace(workspace, standards_source)
        
        # Ensure output directory exists
        outputs_path = Path(workspace.resolve_path_in_workspace(outputs_dir))
        outputs_path.mkdir(parents=True, exist_ok=True)
        
        self.logger.info(f"Single workspace prepared: {workspace.get_isolated_root()}")
    
    def prepare_parallel_workspaces(
        self,
        primary_workspace: WorkspaceSettings,
        extra_workspaces: List[WorkspaceSettings],
        spec_path: str,
        outputs_dir: str,
        standards_source: Optional[str] = None,
        src_dir: str = "src",
    ) -> None:
        """
        Prepare workspaces for parallel agent execution.
        
        This is used for parallel workflows (e.g., code generation with multiple models).
        Process:
        1. Prepare primary workspace (ensure standards are present)
        2. For each extra workspace:
           - Clear src directory if it exists
           - Sync spec_path and outputs_dir from primary workspace
           - Copy standards files
        
        Args:
            primary_workspace: Primary workspace (source of truth for specs/outputs)
            extra_workspaces: Additional workspaces to prepare
            spec_path: Relative path to specifications (e.g., "specifications")
            outputs_dir: Relative path to outputs (e.g., "specflow")
            standards_source: Optional source path for standards files (defaults to /agent/standards)
        """
        self.logger.info(
            f"Preparing parallel workspaces: 1 primary + {len(extra_workspaces)} extra"
        )
        
        # Prepare primary workspace
        self.prepare_single_workspace(primary_workspace, spec_path, outputs_dir, standards_source)

        # Provision the bundled Rosetta plugin (plugin mode) and unpack KB artifacts on the
        # primary. Provisioning is idempotent — run_kb_init_agent already provisioned the
        # primary before KB init so its agent could discover the Rosetta skills.
        self.provision_rosetta_plugin(primary_workspace)
        rosetta_dir = self.settings.ROSETTA_OUTPUT_DIR
        self.unpack_rosetta_artifacts(primary_workspace, rosetta_dir)

        # Prepare each extra workspace
        for i, extra_ws in enumerate(extra_workspaces, start=1):
            self.logger.info(f"Preparing extra workspace {i}/{len(extra_workspaces)}: {extra_ws.get_isolated_root()}")

            # Clear src directory to avoid copying partial work
            self.clear_src_directory(extra_ws, src_dir=src_dir)

            # Sync directories from primary to extra workspace. rosetta/ carries the KB in MCP
            # mode; in plugin mode the agent wrote docs to outputs_dir and CLAUDE.md to the root
            # directly, so both must propagate too (sync_directories handles the CLAUDE.md file
            # and skips it harmlessly when KB init was disabled).
            self.sync_directories(
                source_ws=primary_workspace,
                target_ws=extra_ws,
                directories=[spec_path, outputs_dir, rosetta_dir, _ROSETTA_ROOT_CLAUDE_MD],
            )

            # Copy standards files
            if standards_source:
                self.copy_standards_to_workspace(extra_ws, standards_source)

            # Provision the bundled Rosetta plugin and unpack KB artifacts on each extra
            # workspace so parallel codegen agents discover the same Rosetta toolset.
            self.provision_rosetta_plugin(extra_ws)
            self.unpack_rosetta_artifacts(extra_ws, rosetta_dir)

            ensure_workspace_gitignore(Path(extra_ws.get_isolated_root()))

        self.logger.info("All parallel workspaces prepared")
    
    def copy_standards_to_workspace(
        self,
        workspace: WorkspaceSettings,
        standards_source: str
    ) -> None:
        """
        Copy standards files into a workspace.
        
        Args:
            workspace: Target workspace
            standards_source: Source directory containing standards files
        """
        source = Path(standards_source)
        if not source.exists():
            self.logger.warning(f"Standards source not found: {standards_source}")
            return
        
        # Use STANDARDS_DIR_NAME from settings for consistent directory naming
        target = Path(workspace.resolve_path_in_workspace(self.settings.STANDARDS_DIR_NAME))
        
        self.logger.debug(f"Copying standards from {source} to {target}")
        
        if target.exists():
            # Remove existing standards to ensure fresh copy
            shutil.rmtree(target)
        
        # Copy the entire standards directory
        shutil.copytree(source, target)
        
        self.logger.info(f"Standards copied to {target}")
    
    def _is_workspace_root(self, workspace: WorkspaceSettings, path: Path) -> bool:
        """True when ``path`` is the workspace's own isolated root.

        Used to refuse destructive removal of the root — which would delete the workspace's
        ``.git`` (its ``origin``) and invalidate any process whose cwd is inside it — when
        ``src_dir`` or a sync entry resolves to ``"."``.
        """
        try:
            return path.resolve() == Path(workspace.get_isolated_root()).resolve()
        except OSError:
            return False

    def _base_exclude_patterns(self) -> List[str]:
        """EXCLUDED_ARTIFACT_PATTERNS plus any additive WORKSPACE_EXCLUDE_PATTERNS (env).

        Always includes the hard-preserved names (``.git``) so a workspace's VCS dir is never
        copied between workspaces even if EXCLUDED_ARTIFACT_PATTERNS is overridden to omit it.
        """
        patterns = list(self.settings.EXCLUDED_ARTIFACT_PATTERNS)
        for pat in (*_ALWAYS_PRESERVE_NAMES, *(self.settings.WORKSPACE_EXCLUDE_PATTERNS or [])):
            if pat not in patterns:
                patterns.append(pat)
        return patterns

    def _augment_with_gitignore(
        self, base_patterns: List[str], workspace: WorkspaceSettings
    ) -> List[str]:
        """Union ``base_patterns`` with simple patterns parsed from the workspace's .gitignore.

        Lets a whole-repo (root) sync honor the source repo's own ignores (``.env``, ``.vscode``,
        local-only files) on top of the build/cache SSOT, so those are not propagated into the
        parallel workspaces or the per-workspace remotes.
        """
        patterns = list(base_patterns)
        for pat in read_gitignore_patterns(Path(workspace.get_isolated_root())):
            if pat not in patterns:
                patterns.append(pat)
        return patterns

    def sync_directories(
        self,
        source_ws: WorkspaceSettings,
        target_ws: WorkspaceSettings,
        directories: List[str],
        exclude_patterns: Optional[List[str]] = None,
    ) -> None:
        """Copy each entry in ``directories`` from source to target, skipping excluded build/cache
        patterns. A workspace-root target is overlaid (never removed) so its own ``.git`` survives.
        """
        base_patterns = (
            list(exclude_patterns)
            if exclude_patterns is not None
            else self._base_exclude_patterns()
        )

        for dir_path in directories:
            source_path = Path(source_ws.resolve_path_in_workspace(dir_path))
            target_path = Path(target_ws.resolve_path_in_workspace(dir_path))

            if not source_path.exists():
                self.logger.warning(
                    f"Source directory does not exist, skipping: {source_path}"
                )
                continue

            self.logger.debug(f"Syncing {source_path} -> {target_path}")

            if not source_path.is_dir():
                # Single file: replace any existing target (dir or file) then copy.
                if target_path.exists():
                    if target_path.is_dir():
                        shutil.rmtree(target_path)
                    else:
                        target_path.unlink()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                self.logger.info(f"Synced directory: {dir_path}")
                continue

            is_root_target = self._is_workspace_root(target_ws, target_path)
            patterns = (
                self._augment_with_gitignore(base_patterns, source_ws)
                if is_root_target
                else base_patterns
            )
            ignore = shutil.ignore_patterns(*patterns) if patterns else None

            if is_root_target:
                # Overlay onto the existing root; preserve the target's own .git + excluded dirs.
                shutil.copytree(
                    source_path, target_path, ignore=ignore, dirs_exist_ok=True
                )
            else:
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(source_path, target_path, ignore=ignore)

            self.logger.info(f"Synced directory: {dir_path}")

    def clear_src_directory(self, workspace: WorkspaceSettings, src_dir: str = "src") -> None:
        """
        Clear the source directory in a workspace to give parallel codegen a clean slate.

        Root-safety: when ``src_dir`` resolves to the *workspace root* (brownfield runs where the
        whole repo is the source, i.e. ``src_dir="."``), the root is NEVER ``rmtree``'d — that
        would destroy the workspace's own ``.git`` (its ``origin``) and invalidate any process
        whose cwd is inside it. Instead only non-excluded children are removed, preserving
        ``.git``, dependency caches, and gitignored files.

        Args:
            workspace: Workspace to clear.
            src_dir: Name of the source directory to clear (default: "src").
        """
        src_path = Path(workspace.resolve_path_in_workspace(src_dir))

        if not (src_path.exists() and src_path.is_dir()):
            self.logger.debug(f"No src directory to clear: {src_path}")
            return

        if self._is_workspace_root(workspace, src_path):
            self._clear_workspace_root_contents(workspace, src_path)
            return

        self.logger.info(f"Clearing src directory: {src_path}")
        shutil.rmtree(src_path)
        self.logger.info("Src directory cleared")

    def _clear_workspace_root_contents(
        self, workspace: WorkspaceSettings, root_path: Path
    ) -> None:
        """Remove non-excluded children of the workspace root, preserving .git and caches.

        Preserved set = the base exclude patterns + this workspace's own ``.gitignore``
        (best-effort). ``.git`` is preserved unconditionally via ``_ALWAYS_PRESERVE_NAMES``,
        independent of how EXCLUDED_ARTIFACT_PATTERNS is configured. This set is close to (but not
        guaranteed identical to) what the root re-sync skips — the re-sync reads the *source*
        workspace's ``.gitignore`` while this reads the *target*'s — but since the overlay is
        non-destructive and ``.git`` is always preserved, any difference is benign.
        """
        patterns = self._augment_with_gitignore(self._base_exclude_patterns(), workspace)
        match = shutil.ignore_patterns(*patterns) if patterns else None
        children = list(root_path.iterdir())
        preserved = (
            match(str(root_path), [c.name for c in children]) if match else set()
        )
        # Hard invariant: never remove the VCS dir, even if config/ignore_patterns omit it.
        preserved = set(preserved) | _ALWAYS_PRESERVE_NAMES

        removed = 0
        for child in children:
            if child.name in preserved:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1

        self.logger.info(
            f"Cleared {removed} non-excluded entries from workspace root {root_path}; "
            "preserved .git, caches, and gitignored files"
        )
    
    async def _stage_and_commit(self, ws_path: Path, commit_message: str) -> str:
        """Stage all changes and commit, using --allow-empty if the tree is clean.

        Returns the new commit SHA.  Raises GitCommandError on failure (hard path).
        """
        await run_git(ws_path, ["add", "-A"])
        staged = await run_git(ws_path, ["diff", "--cached", "--name-only"])
        if staged:
            await run_git(ws_path, ["commit", "-m", commit_message])
        else:
            await run_git(ws_path, ["commit", "--allow-empty", "-m", commit_message])
        return await run_git(ws_path, ["rev-parse", "HEAD"])

    async def commit_and_push_baseline(
        self,
        workspace: WorkspaceSettings,
        commit_message: str = "SKIP_generation_baseline",
    ) -> None:
        """
        Create a single orphan commit containing all current workspace files and
        force-push it as origin/main, replacing any prior history on both the
        local repo and the remote.

        This is intentionally destructive: at baseline time no generation has
        started, so there is no code worth preserving.  Starting from a single
        orphan commit guarantees that git-log only ever sees agent commits —
        zero noise from previous generations.

        Idempotent: safe to retry after a partial failure at any point in the
        sequence.  The function detects which git state it is resuming from and
        continues where it left off rather than restarting from scratch.

        Working-tree files are NEVER deleted — git checkout --orphan preserves
        the working tree intact; only git history is rewritten.

        Args:
            workspace: Workspace to commit and push.
            commit_message: Subject line for the commit (defaults to
                ``SKIP_generation_baseline`` so it is excluded from P10Y).
        """
        ws_path = Path(workspace.resolve_path_in_workspace("."))
        _TMP = "_baseline_tmp"

        # ── Phase 1: land on _TMP with exactly one commit ──────────────────
        #
        # Detect where we are so a partial prior run can be resumed cleanly.
        # Possible states on entry:
        #   A) On WORKSPACE_DEFAULT_BRANCH (normal start, or resumed after
        #      checkout-main but before push)
        #   B) On _TMP, no commit yet  (killed after checkout --orphan)
        #   C) On _TMP, commit exists  (killed after commit but before branch -f)
        #   D) On some other branch    (unusual; treat like A)
        try:
            current_branch = await run_git(
                ws_path, ["rev-parse", "--abbrev-ref", "HEAD"]
            )
        except GitCommandError:
            current_branch = ""  # detached HEAD or repo has no commits at all

        if current_branch == _TMP:
            # States B or C — we're already on the orphan branch.
            # Use --verify so git exits non-zero on an unborn branch (no commits).
            try:
                commit_sha = await run_git(
                    ws_path, ["rev-parse", "--verify", "HEAD"]
                )
                self.logger.info(
                    f"Resuming baseline: existing orphan commit {commit_sha[:12]}"
                    f" on {_TMP} in {ws_path}"
                )
            except GitCommandError:
                # State B: unborn branch — the checkout --orphan succeeded but
                # commit did not.  Stage everything and commit now.
                commit_sha = await self._stage_and_commit(ws_path, commit_message)
                self.logger.info(
                    f"Resumed orphan baseline commit in {ws_path}: {commit_message!r}"
                )
        else:
            # States A or D — we are NOT on _TMP.
            # Safe to delete any leftover _TMP (we are not checked out to it).
            try:
                await run_git(ws_path, ["branch", "-D", _TMP])
                self.logger.debug(f"Cleaned up leftover {_TMP} branch in {ws_path}")
            except GitCommandError:
                pass  # Branch did not exist — that is fine.

            # Create an orphan branch and commit the full working tree.
            # checkout --orphan preserves all working-tree files; nothing is deleted.
            await run_git(ws_path, ["checkout", "--orphan", _TMP])
            commit_sha = await self._stage_and_commit(ws_path, commit_message)
            self.logger.info(
                f"Orphan baseline commit in {ws_path}: {commit_message!r}"
            )

        # ── Phase 2: point WORKSPACE_DEFAULT_BRANCH at the orphan commit ───
        #
        # `branch -f` requires we are NOT on the target branch.  We are on
        # _TMP here (either we just created it, or we resumed from state B/C),
        # so this is always safe.
        await run_git(
            ws_path, ["branch", "-f", WORKSPACE_DEFAULT_BRANCH, commit_sha]
        )
        await run_git(ws_path, ["checkout", WORKSPACE_DEFAULT_BRANCH])

        # _TMP may already be gone if a prior partial run deleted it; ignore.
        try:
            await run_git(ws_path, ["branch", "-D", _TMP])
        except GitCommandError:
            pass

        # ── Phase 3: force-push (idempotent — safe to retry) ───────────────
        await run_git(
            ws_path, ["push", "--force", "origin", WORKSPACE_DEFAULT_BRANCH]
        )
        self.logger.info(
            f"Pushed orphan baseline to origin/{WORKSPACE_DEFAULT_BRANCH} for {ws_path}"
        )

    async def commit_and_push_outstanding(
        self,
        workspace: WorkspaceSettings,
        commit_message: str,
    ) -> None:
        """
        Stage any uncommitted work left by agents and push to origin.

        NON-DESTRUCTIVE: adds a commit on top of the existing history,
        preserving all prior agent commits.  Use this after generation
        phases complete (janitor step), NOT for the pre-generation baseline.

        Idempotent: if there is nothing uncommitted, skips the commit and
        still pushes so that origin/main is up to date.

        Args:
            workspace: Workspace whose outstanding work should be committed.
            commit_message: Subject line for the commit.
        """
        ws_path = Path(workspace.resolve_path_in_workspace("."))

        try:
            await run_git(ws_path, ["add", "-A"])
        except GitCommandError as e:
            self.logger.warning(f"git add -A failed in {ws_path}: {e.stderr}")

        try:
            staged = await run_git(ws_path, ["diff", "--cached", "--name-only"])
        except GitCommandError:
            staged = ""

        if staged:
            try:
                await run_git(ws_path, ["commit", "-m", commit_message])
                self.logger.info(
                    f"Committed outstanding agent work in {ws_path}: {commit_message!r}"
                )
            except GitCommandError as e:
                self.logger.warning(f"Commit failed in {ws_path}: {e.stderr}")
        else:
            self.logger.info(f"No outstanding changes to commit in {ws_path}")

        await run_git(ws_path, ["push", "origin", WORKSPACE_DEFAULT_BRANCH])
        self.logger.info(
            f"Pushed to origin/{WORKSPACE_DEFAULT_BRANCH} for {ws_path}"
        )

    def unpack_rosetta_artifacts(
        self,
        workspace: WorkspaceSettings,
        rosetta_dir: str = "rosetta",
    ) -> None:
        """Unpack rosetta/ output into workspace root for Claude Code SDK discovery.

        Copies contents of rosetta/ into the workspace root, preserving structure:
          rosetta/CLAUDE.md   -> workspace_root/CLAUDE.md
          rosetta/agents/     -> workspace_root/.claude/agents/
          rosetta/skills/     -> workspace_root/.claude/skills/
          rosetta/commands/   -> workspace_root/.claude/commands/
          rosetta/docs/       -> workspace_root/docs/

        The agents/, skills/, and commands/ remappings exist because the SDK's
        sensitive-file guard blocks agent writes under `.claude/` — so the KB init
        agent stages those trees under rosetta/ and we remap here during unpack.

        No-op if rosetta/ doesn't exist (KB init was skipped or failed).

        Unpack is an **MCP-mode** operation: the KB-init agent stages everything under rosetta/
        because it cannot write `.claude/` directly. In plugin mode the plugin trees are copied
        into `.claude/` by ``provision_rosetta_plugin`` and the agent writes its docs to their
        final locations directly, so there is nothing to unpack — this returns early.
        """
        if self._plugin_mode_active():
            self.logger.debug(
                "Plugin mode active — skipping rosetta/ unpack "
                f"({workspace.get_isolated_root()}); plugin provisions .claude/ directly"
            )
            return

        rosetta_path = Path(workspace.resolve_path_in_workspace(rosetta_dir))
        if not rosetta_path.exists():
            self.logger.debug(
                f"No {rosetta_dir}/ directory in "
                f"{workspace.get_isolated_root()}, skipping unpack"
            )
            return

        ws_root = Path(workspace.get_isolated_root())

        for item in rosetta_path.iterdir():
            if item.is_dir() and item.name in _ROSETTA_DOT_CLAUDE_SUBDIRS:
                target = ws_root / ".claude" / item.name
            else:
                target = ws_root / item.name
            if item.is_dir():
                # Merge into existing directory (dirs_exist_ok) rather than replacing.
                # Planning files written before this call (e.g. IMPLEMENTATION_PLAN.md,
                # e2e-test-plan.md in docs/) are preserved; rosetta files are added/updated
                # alongside them. Replacing via rmtree would wipe planning outputs when
                # rosetta/docs/ and the outputs_dir share the same name ("docs").
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

        self.logger.info(f"Unpacked rosetta/ artifacts to {ws_root}")

    def _plugin_mode_active(self) -> bool:
        """True when the provisioned Rosetta plugin (not the live MCP) supplies the KB.

        Delegates to the single mode resolver in ``app.core.rosetta_kb`` so this and every other
        site keyed on the mode agree. In provisioned-plugin mode ``provision_rosetta_plugin``
        copies the plugin trees into ``.claude/`` and the KB-init agent writes its docs to final
        locations directly, so the MCP-mode ``unpack_rosetta_artifacts`` staging remap must not
        run. With KB DISABLED this is False — unpack then runs but no-ops because rosetta/ was
        never created.
        """
        return resolve_rosetta_kb_mode(self.settings) is RosettaKbMode.PROVISIONED_PLUGIN

    def provision_rosetta_plugin(self, workspace: WorkspaceSettings) -> bool:
        """Copy the bundled Rosetta plugin's discovery trees into a workspace.

        Provisioned-plugin mode (the default) ships the Rosetta agents/skills/commands/hooks with
        the image at ``settings.ROSETTA_PLUGIN_PATH`` instead of fetching them from the live
        ims-mcp service. Only the discovery-shaped trees are copied into the workspace (project
        scope cannot read ``/opt`` or ``~/.claude``):

            <plugin>/agents/    -> .claude/agents/
            <plugin>/skills/    -> .claude/skills/
            <plugin>/workflows/ -> .claude/commands/      (plugin.json commands: ./workflows/)
            <plugin>/hooks/hooks.json -> merged into .claude/settings.json "hooks"

        The plugin's other files (hook scripts, rules/, templates/) stay in the read-only image
        and are reached via ``CLAUDE_PLUGIN_ROOT`` = ``settings.ROSETTA_PLUGIN_PATH``, set per
        agent in ``claude_code.setup_rosetta_plugin_env``. The active bundled hooks are inline
        commands, so they run as CLI subprocesses without needing an in-workspace plugin copy.

        Idempotent: re-copy merges (``dirs_exist_ok``) and hook merge de-duplicates. Returns
        False (no-op) when the mode resolver reports anything other than PROVISIONED_PLUGIN — i.e.
        the live MCP is enabled (it supplies the KB instead), or ``ROSETTA_PLUGIN_PATH`` is unset
        or missing on disk (e.g. an image without the plugin).
        """
        plugin_root = rosetta_plugin_root(self.settings)
        if plugin_root is None:
            if not self.settings.ROSETTA_MCP_ENABLED and (self.settings.ROSETTA_PLUGIN_PATH or "").strip():
                # Configured to use the plugin but the path isn't a directory — surface it.
                self.logger.warning(
                    "ROSETTA_PLUGIN_PATH not found, skipping plugin provisioning: "
                    f"{self.settings.ROSETTA_PLUGIN_PATH}"
                )
            return False
        src = Path(plugin_root)

        claude_dir = Path(workspace.get_isolated_root()) / ".claude"

        # 1. Surface agents/skills/commands for setting_sources=["project"] discovery.
        #    The source dir names follow the plugin's own plugin.json layout.
        for src_name, dest_name in self._resolve_plugin_claude_map(src):
            src_sub = src / src_name
            if not src_sub.is_dir():
                continue
            dest_sub = claude_dir / dest_name
            dest_sub.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_sub, dest_sub, dirs_exist_ok=True)

        # 2. Register plugin hooks into project settings so they fire under project scope.
        self._merge_plugin_hooks(src, claude_dir)

        self.logger.info(f"Provisioned Rosetta plugin from {src} into {claude_dir}")
        return True

    def _resolve_plugin_claude_map(self, plugin_root: Path) -> List[Tuple[str, str]]:
        """Resolve ordered (plugin source dir, workspace ``.claude/`` dest) pairs from the manifest.

        A Claude Code plugin may relocate its command/agent trees via ``plugin.json``
        (``"commands"`` / ``"agents"``, defaulting to ``./commands/`` and ``./agents/``);
        Skills are discovered from the conventional ``skills/`` dir. Reading the manifest keeps
        the copy in step with the plugin's declared layout instead of a hardcoded guess — e.g.
        the Rosetta plugin declares ``"commands": "./workflows/"``. Falls back to
        ``_ROSETTA_PLUGIN_CLAUDE_MAP`` on a missing/unparseable manifest; per-entry, a non-string
        path value falls back to that entry's conventional dir.

        Returns ordered pairs (not a dict) so two sources resolving to the same name can't
        collide and silently drop a tree.
        """
        manifest_file = plugin_root / ".claude-plugin" / "plugin.json"
        try:
            manifest = json.loads(manifest_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning(
                f"Could not read plugin manifest {manifest_file}, using default layout "
                f"(non-fatal): {exc}"
            )
            return list(_ROSETTA_PLUGIN_CLAUDE_MAP)

        def _src_dir(value: object, default: str) -> str:
            """Normalize a manifest path (``./workflows/`` -> ``workflows``) to a source dir name."""
            if not isinstance(value, str) or not value.strip():
                return default
            normalized = value.strip()
            if normalized.startswith("./"):
                normalized = normalized[2:]
            normalized = normalized.rstrip("/")
            return normalized or default

        agents_src = _src_dir(manifest.get("agents"), "agents")
        commands_src = _src_dir(manifest.get("commands"), "commands")
        return [(agents_src, "agents"), ("skills", "skills"), (commands_src, "commands")]

    def _merge_plugin_hooks(self, plugin_root: Path, claude_dir: Path) -> None:
        """Merge the plugin's ``hooks/hooks.json`` "hooks" block into ``.claude/settings.json``.

        Existing settings are preserved; plugin hook arrays are appended per event without
        duplicating an identical entry. ``${CLAUDE_PLUGIN_ROOT}`` is left literal and resolved
        at runtime via the per-agent ``CLAUDE_PLUGIN_ROOT`` env var (see
        ``claude_code.setup_rosetta_plugin_env``). Non-fatal on any I/O or parse error.
        """
        hooks_file = plugin_root / "hooks" / "hooks.json"
        if not hooks_file.is_file():
            return
        try:
            plugin_hooks = json.loads(hooks_file.read_text()).get("hooks", {})
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning(f"Could not read plugin hooks {hooks_file} (non-fatal): {exc}")
            return
        if not plugin_hooks:
            return

        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.json"
        settings_data: dict = {}
        if settings_file.is_file():
            try:
                settings_data = json.loads(settings_file.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                self.logger.warning(
                    f"Could not read {settings_file}, recreating (non-fatal): {exc}"
                )
                settings_data = {}

        # Defensive merge: a malformed settings.json "hooks" block, a non-list event value, or
        # an unwritable settings file must not abort workspace provisioning (see docstring).
        merged_hooks = settings_data.get("hooks")
        if not isinstance(merged_hooks, dict):
            merged_hooks = {}
            settings_data["hooks"] = merged_hooks
        for event, entries in plugin_hooks.items():
            if not isinstance(entries, list):
                continue
            existing = merged_hooks.get(event)
            if not isinstance(existing, list):
                existing = []
                merged_hooks[event] = existing
            for entry in entries:
                if entry not in existing:
                    existing.append(entry)

        try:
            settings_file.write_text(json.dumps(settings_data, indent=2))
        except OSError as exc:
            self.logger.warning(f"Could not write {settings_file} (non-fatal): {exc}")

    def sync_plan_to_workspaces(
        self,
        primary_workspace: WorkspaceSettings,
        extra_workspaces: List[WorkspaceSettings],
        plan_file_path: str
    ) -> None:
        """
        Synchronize IMPLEMENTATION_PLAN.md from primary workspace to all extra workspaces.
        
        This ensures all workspaces have the same implementation plan before phase execution begins.
        
        Args:
            primary_workspace: Primary workspace containing the plan file
            extra_workspaces: List of extra workspaces to sync the plan to
            plan_file_path: Relative path to IMPLEMENTATION_PLAN.md (e.g., "specflow/IMPLEMENTATION_PLAN.md")
        """
        source_path = Path(primary_workspace.resolve_path_in_workspace(plan_file_path))
        
        if not source_path.exists():
            self.logger.warning(
                f"Plan file does not exist in primary workspace: {source_path}"
            )
            return
        
        self.logger.info(
            f"Syncing plan file from {source_path} to {len(extra_workspaces)} workspaces"
        )
        
        for i, extra_ws in enumerate(extra_workspaces, start=1):
            target_path = Path(extra_ws.resolve_path_in_workspace(plan_file_path))
            
            # Ensure target directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Copy the plan file
            shutil.copy2(source_path, target_path)
            
            self.logger.info(
                f"Synced plan to workspace {i}/{len(extra_workspaces)}: {target_path}"
            )
        
        self.logger.info("Plan synchronization complete")