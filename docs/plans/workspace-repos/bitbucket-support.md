# Add BitBucket Support to SpecFlow

## Context

SpecFlow is hardwired to **GitHub** end-to-end with **no git-hosting-provider abstraction**. GitHub-specific logic is scattered across credential resolution, authenticated-URL construction, token sanitization, repo provisioning, deploy prompts (GitHub Actions + `gh` CLI), onboarding, and notifications. The only pre-existing notion of provider variance is a dormant `GitType` enum (`GITHUB`/`GITLAB`) in the P10Y models. `bitbucket` appears nowhere except the branch name — this is greenfield.

We need to add BitBucket as a first-class git host. **Scope decisions (confirmed with user):**
1. **Full parity**: git hosting AND CI/CD (deploy/E2E).
2. **Both BitBucket Cloud (bitbucket.org) and Server/Data Center (self-hosted).**
3. **Auth = Access Tokens** (Repository/Workspace access tokens), not app passwords.
4. **Per-generation/per-key coexistence**: GitHub and BitBucket usable side-by-side; provider resolved per workspace / per API-key, mirroring the existing per-key encrypted-token model.

**Outcome**: a `GitProvider` abstraction (Open/Closed + SRP) that generalizes the three currently-hardcoded single-source mechanisms — authenticated-URL builder, token sanitizer, deploy recipe — *in place*, plus new BitBucket implementations, so a workspace on any provider can clone/commit/push/archive, be provisioned, and (Cloud) deploy via Pipelines.

## ⚠️ Design decision to confirm during execution: Server/DC CI/CD

BitBucket **Server/Data Center has no native CI** (no Pipelines). "Full CI/CD parity" is achievable only for **Cloud** (Pipelines via REST API). For **Server/DC**, the recommended design **degrades gracefully**: git-hosting + codegen fully work, but the deploy/E2E loop is **skipped with a structured USER NOTICE** (reusing the existing DEPLOY FAILURE REPORT / Slack channel) telling the operator that Server/DC deploy needs external CI (Jenkins/Bamboo) SpecFlow doesn't own. A future `GenericWebhookDeployStrategy` (hitting a configured `DEPLOY_WEBHOOK_URL`) can slot behind the same interface without reopening the work. This plan proceeds on that basis.

## The Abstraction

New package `backend/app/services/git_providers/`, SRP-split across the three layers it touches (git subprocess, provisioning script, deploy/prompt). One `Enum` selects implementations via a registry.

- `enums.py` — `GitProvider(str, Enum)`: `GITHUB`, `BITBUCKET_CLOUD`, `BITBUCKET_SERVER`.
- `hosting.py` — `GitHostingProvider(ABC)`: `authenticated_clone_url(repo_url, auth)`, `token_sanitization_patterns()`, `branch_web_url(repo_url, branch)`, `default_git_user()`.
- `provisioning.py` — `RepoProvisioner(ABC)`: `repository_exists`, `create_repository`, `grant_write_access`, `get_authenticated_actor`, `web_url_for`.
- `deploy_strategy.py` — `DeployStrategy(ABC)`: `supports_managed_ci` (ClassVar bool), `deploy_env(token)`, `build_deploy_recipe(ctx)`, `unsupported_ci_notice(ctx)`.
- `registry.py` — `hosting_for`, `deploy_strategy_for`, `provisioner_for`, `provider_from_repo_url(repo_url)` (host-based default: `bitbucket.org`→CLOUD, `github.com`→GITHUB, else SERVER-requires-explicit-config).
- `github.py`, `bitbucket.py` — concrete impls.

Provider-neutral pieces to **REUSE unchanged**: `git_utils.run_git`, `git_archive_service.GitArchiveService` (operate on `origin`), per-workspace `repo_url` storage.

**Persistence / back-compat** (must read existing data with zero migration required):
- Workspace doc gains `git_provider` (nullable; read fallback = `provider_from_repo_url(repo_url)` → `github` for existing URLs). Server/DC workspaces MUST carry it explicitly (host isn't inferable).
- API-key doc: read ciphertext new→legacy (`git_token_ciphertext` → `github_token_ciphertext`); write only new. Add `git_provider` on key doc (default `github` for legacy). Keep `PUT /api/v1/auth/github-token` route + add alias `PUT /api/v1/auth/git-token`; thin re-export shim for renamed modules for one release.

## Phased Implementation

Order deliberately lands **git-hosting before CI/CD** (CI/CD is riskiest).

### Phase 0 — Scaffolding + GitHub-first (no behavior change)
Create the `git_providers/` package + interfaces + `GitHubProvider` that *moves* today's exact logic (the `user:token@` URL builder and `ghp_`/`ghs_`/`github_pat_`+`@github.com` regexes out of `workspace_pool.py`; the `{"GH_TOKEN": token}` env and `gh workflow run` recipe out of `agents_claude_code.py`).
- **Golden/characterization tests** (`backend/tests/unit/git_providers/test_github_provider.py`) assert byte-identical output vs current behavior — makes Phases 2 & 5 provably non-regressive.

### Phase 1 — Persistence + auth/secret generalization
- Rename `backend/app/core/github_platform_secrets.py` → `git_platform_secrets.py` (keep re-export shim). `GithubPlatformSecrets` → `GitPlatformSecrets` holding Fernet + `dict[GitProvider, DefaultIdentity]`. New settings `GIT_TOKEN_DEFAULT_BITBUCKET` + K8s key `K8S_SECRET_KEY_BITBUCKET_DEFAULT`; keep `GITHUB_TOKEN_DEFAULT`/`GITHUB_TOKEN` aliases.
- `backend/app/services/github_auth.py` → generalize to `git_auth.py`: `GithubAuthContext` → `GitAuthContext(git_user_name, token, provider)`. `resolve_git_auth_for_api_key_document(doc, workspace_provider, secrets)` preserves order (per-key ciphertext → per-provider default → error). `github_cli_env_for_generation` → `deploy_env_for_generation` returning `strategy.deploy_env(token)`. Keep old names as deprecated aliases to stage callsite churn.
- Modify `backend/app/core/config.py` (new settings/K8s keys + aliases), `backend/app/core/app_lifecycle.py` (renamed init), `backend/app/api/v1/auth.py` (`GitTokenUploadBody`, write `git_token_ciphertext`+`git_provider`, accept old shape).
- Idempotent, read-safe migration `backend/scripts/migrate_git_provider.py` (backfill workspace `git_provider`; copy ciphertext field). Not required for reads.
- Tests: auth-resolution matrix (legacy-only / new-only / per-provider default / error), secrets loader from env + mocked K8s map, both providers.

### Phase 2 — Clone / push / sanitize generalization (git-hosting parity lands)
- `backend/app/services/workspace_pool.py`: `_get_authenticated_repo_url` delegates to `hosting_for(auth.provider).authenticated_clone_url(...)` (delete inline string surgery). `_sanitize_token_in_message` keeps explicit-token replacement, then applies patterns from **all** registered providers (defensive — never leak even on misresolution). Every `resolve_github_auth_for_generation_id(...)` callsite → `resolve_git_auth_for_generation_id(...)` (loads workspace `git_provider`). Clone/init/archive/reset funcs otherwise unchanged (operate on `origin`/`run_git`). GitHub-specific auth-failure hint string becomes provider-parameterized.
- Create `backend/app/services/git_providers/bitbucket.py`: `BitbucketCloudProvider` (clone `https://x-token-auth:{token}@bitbucket.org/{ws}/{repo}.git`; branch `/src/{branch}`; sanitize `@bitbucket.org`, `x-token-auth:`, `ATCTT` prefix) and `BitbucketServerProvider` (clone `https://x-token-auth:{token}@{host}/scm/{proj}/{repo}.git`; branch `/browse?at=refs%2Fheads%2F{branch}`; host from `BITBUCKET_SERVER_BASE_URL`).
- Tests: URL injection + idempotence, BB sanitization shapes, branch-url builders, clone flow with BB workspace doc (mock `run_git`).

### Phase 3 — Repo provisioning
- `backend/scripts/create_generation_session_repos.py`: extract `GitHubAPIClient` to `RepoProvisioner` (in `github.py`); add `BitbucketCloudProvisioner` (`https://api.bitbucket.org/2.0`, `POST /repositories/{ws}/{repo}`, permission endpoints, `Bearer` access token) and `BitbucketServerProvisioner` (`{base}/rest/api/1.0/projects/{key}/repos`). Add `--git-provider` flag; generalize `--github-org`/`--team` → `--namespace`/`--principal` (keep aliases). Emit `git_provider` + `provisioner.web_url_for(name)` as `repo_url` (no hardcoded `github.com`). `_normalize_git_url` unchanged.
- Tests: provisioner unit tests (mock `httpx`) per provider; workspace-doc emission.

### Phase 4 — P10Y + notifications (small, low-risk)
- `backend/app/services/p10y/p10y_api_models.py`: add `GitType.BITBUCKET = "bitbucket"`; set `RepositoryDetails.git_provider` for BB repos.
- `backend/app/core/notifications.py` (~line 1144): replace hardcoded `{repo_url}/tree/{branch}` with `hosting_for(provider).branch_web_url(...)` (resolve provider from workspace doc). Third single-source site — generalize, don't branch beside it.

### Phase 5 — CI/CD layer (riskiest — last)
Bifurcation: **GitHub** = existing `gh` recipe (now inside `GitHubProvider`); **BB Cloud** = `curl`+`jq` recipe (`POST /2.0/repositories/{ws}/{repo}/pipelines/` custom selector, poll `GET .../pipelines/{uuid}` for `state.name==COMPLETED`, config `bitbucket-pipelines.yml`) — curl+jq already in `bash_usage`, no new tooling; **BB Server/DC** = `supports_managed_ci=False`, deploy skipped with USER NOTICE (see top-of-plan design decision).
- `backend/app/schemas/deploy_context.py`: `DeployGithubContext` → `DeployContext(repo, ref, deploy_workflow, provider)` (keep alias).
- `backend/app/services/workflow_steps.py` `_build_deploy_github_context` (~1064): read workspace `git_provider`; if `not supports_managed_ci`, emit notice + skip (generalize the `github_repo` guard at ~1176 to capability+repo).
- `backend/app/prompts/agents_claude_code.py` `generate_deploy_phase_agent_template` (~1139): replace the hardcoded GitHub-Actions bash block with `strategy.build_deploy_recipe(ctx)` (GitHub block moves verbatim into `GitHubDeployStrategy`; BB Cloud block new). Planning-side text (~741) becomes provider-agnostic.
- `backend/app/services/claude_code.py` (~1178): `deploy_extra_env = deploy_env_for_generation(...)`.
- `backend/app/core/tool_usage.py`: add `deploy_extra_tools_for(provider)` gating `Bash(gh:*)` to GitHub deploys (BB uses curl/jq).
- `backend/app/services/agent_hooks.py` `_ci_blocklist`: verify BB `for poll in seq` loop isn't caught by `_watch_flag_blocklist`; add a `gh run watch` analogue only if a BB hang command exists.
- `backend/app/standards/deployment_standards.md`: add BB Pipelines section paralleling GitHub Actions; default artifacts gain `bitbucket-pipelines.yml`; note Server/DC has no managed CI.
- Tests: `build_deploy_recipe` golden per provider; `supports_managed_ci=False` produces notice + skips; `deploy_env` var; `deploy_extra_tools_for` gating.

### Phase 6 — Onboarding / TUI / config / docs
- `mcp_server/tui/onboarding.py`: generalize hardcoded `_GITHUB` step into a provider-selecting step (GitHub / BB Cloud / BB Server) collecting `GIT_TOKEN`, namespace (org/workspace/project), `GIT_USER_NAME`, Server base URL. Mirror the LLM `_PROVIDER` pattern.
- `mcp_server/tui/config.py`: register new keys in `ENV_SECRET_KEYS`/`MASKED_KEYS` (load-time parity assertion is a hard gate).
- `.env.quickstart.example`, `QUICKSTART.md`, `specflow-init.sh` (`_GH_ORG` → provider-parameterized): add BB variants.
- MCP forwarding stays provider-agnostic (provider resolved server-side per workspace/key); forward a `GIT_PROVIDER` hint only if onboarding must set a per-key default.
- Tests: `mcp_server` config parity assertion; onboarding validation per provider.

## Biggest Risks
1. **Server/DC has no managed CI** — graceful degradation, not a fake pipeline. Needs sign-off that this satisfies requirement #1 for Server/DC (see top-of-plan).
2. **Exact BB access-token clone/REST forms** differ Cloud vs Server (`x-token-auth`; `/scm/` path; `Bearer` REST). Isolated by the abstraction but need live validation against a real BB instance — `authenticated_clone_url` for Server is highest-uncertainty.
3. **Token-leak prevention is a security control** — apply all providers' sanitization patterns; Phase 0 golden tests must prove GitHub scrubbing unchanged; BB patterns (`x-token-auth:`, `ATCTT…`) must be added or BB tokens leak into logs/Slack.
4. **Field-rename back-compat** — `github_token_ciphertext` + `github-token` route are in production data/clients; read-fallback + route alias + one-release shim + idempotent non-destructive migration are mandatory.
5. **Deploy recipe is autonomous prompt text (up to 30 min)** — BB Cloud curl/jq poll loop must have the same hard timeout + non-fatal-403 semantics as GitHub and must not trip `_ci_blocklist`, else deploys hang mid-run with no human in the loop.

## Verification
- `make unit-tests` after each phase; pass count ≥ baseline (record baseline first). New `backend/tests/unit/git_providers/` package: one module per provider per interface + registry/host-derivation test. Clone/deploy flow tests use in-memory DB + mocked `run_git`/`httpx` (no network). Do NOT use `make integration-tests` for these.
- `make check-complexity-diff` after Phases 2 and 5 (the two that refactor large existing files).
- End-to-end sanity: run `create_generation_session_repos.py --git-provider bitbucket_cloud --dry-run` to confirm provisioning wiring; exercise a BB Cloud workspace through clone→commit→push→archive with a real (or sandbox) BB access token; confirm the deploy recipe triggers a Pipelines custom run and polls to completion. Confirm GitHub path is byte-identical (golden tests green) — no regression.
