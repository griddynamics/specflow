# BitBucket Cloud support — commit, read (P10Y), and auto-provision

> Standalone, **fully working** first delivery for BitBucket. It unlocks the
> prioritized capability end-to-end: SpecFlow agents clone/commit/push to a
> BitBucket Cloud repo, P10Y reads those commits, and the repos are
> **auto-provisioned** (no manual repo creation). CI/CD (deploy/E2E),
> Server/Data-Center, and the full `git_providers/` refactor + `github_*`→`git_*`
> renames remain out of scope (see `bitbucket-support.md`).
>
> Delivered as **two phases in one shippable change** — Phase A (credentials +
> runtime), Phase B (auto-provisioning). Both land together so the result is a
> working deployment, not a half-path.

## ⚠️ Core assumption — a single active provider, no mixing

**A deployment is either all-GitHub or all-BitBucket. Never mixed.** All
workspaces in the pool share one git host. This is a hard assumption and it
simplifies everything: the provider is resolved **once, globally**, from which
token is configured — not per workspace, not per key. There is no coexistence
path to build or test.

- Active provider = explicit `GIT_PROVIDER` setting if set; else inferred from
  **exactly one** of `GITHUB_TOKEN` / `BITBUCKET_TOKEN` being present.
- Both set with no explicit `GIT_PROVIDER`, or neither set → **startup error**
  (ambiguous / unconfigured). Fail fast, don't guess.
- A GitHub-only deployment (only `GITHUB_TOKEN` set) resolves to GitHub and is
  byte-identical to today — full back-compat, zero migration.

## The key realization (why this is small)

The **clone/commit/push runtime and the entire P10Y commit-reading path are
already provider-neutral**. Almost nothing that moves git data is
GitHub-specific:

- `WorkspacePool._get_authenticated_repo_url` (`workspace_pool.py:1377`) splices
  `https://{git_user_name}:{token}@{rest}` into **any** https URL — no
  `github.com` anywhere. With `git_user_name="x-token-auth"` and a BitBucket URL
  it already emits the exact Cloud access-token form
  `https://x-token-auth:{token}@bitbucket.org/{ws}/{repo}.git`.
- All commit/push/archive go through `run_git` on `origin` (`git_utils.py`) —
  host-agnostic.
- `GithubAuthContext` (`github_auth.py:16`) is just `(git_user_name, token)`;
  Fernet encrypt/decrypt is opaque.
- **P10Y needs zero code change.** It reads commits two ways, both neutral:
  local `git log --format=%H\t%s` (`p10y_lib.py:266`), and the external Compass
  API keyed by an **integer** `repository_id` (`p10y_api_client.py`). The
  `GitType` enum is dead code. Compass supports BitBucket natively; connecting it
  to the BitBucket repo is a **user setup step** (Compass side, outside SpecFlow)
  — same as the GitHub case today. SHAs match because both read the same repo.

So the real GitHub-specificity to address is just: credential selection,
`git_user_name`, token log-sanitization, and repo provisioning.

## User experience (paste one token)

In the TUI / `.env`, below `GITHUB_TOKEN`, add `BITBUCKET_TOKEN` and
`BITBUCKET_WORKSPACE`. A BitBucket deployment sets those (and leaves
`GITHUB_TOKEN`/`GITHUB_ORG` unset); a GitHub deployment does the reverse. The
user pastes a **BitBucket Cloud Repository/Workspace Access Token** plus the
workspace slug — **no username** (access tokens always use the fixed
`x-token-auth` actor). Run the provisioning script; workspaces are created on
BitBucket and generation runs. That's it.

---

## Phase A — credentials + runtime

### 1. Minimal provider abstraction (new module)
`backend/app/services/git_provider.py` — one small file (the full
`git_providers/` package split is a later refactor PR):

```python
class GitProvider(str, Enum):
    GITHUB = "github"
    BITBUCKET_CLOUD = "bitbucket_cloud"

@dataclass(frozen=True)
class GitHostStrategy:
    provider: GitProvider
    default_git_user: str                       # x-access-token / x-token-auth
    def sanitization_patterns(self) -> list[tuple[re.Pattern, str]]: ...

_GITHUB    = GitHostStrategy(GitProvider.GITHUB, "x-access-token", ...)          # ghp_/gh[ps]_/github_pat_/@github.com
_BITBUCKET = GitHostStrategy(GitProvider.BITBUCKET_CLOUD, "x-token-auth", ...)   # ATCTT…/x-token-auth:/@bitbucket.org

def strategy_for(provider: GitProvider) -> GitHostStrategy: ...
def all_strategies() -> list[GitHostStrategy]: ...
def resolve_active_git_provider(settings: Settings) -> GitProvider: ...  # the global switch (see assumption)
```
- Move the existing GitHub regexes verbatim into `_GITHUB.sanitization_patterns()`;
  add a golden test asserting GitHub scrubbing output is byte-identical to today.
- `resolve_active_git_provider` implements the exactly-one-token rule above.

### 2. Settings — `BITBUCKET_TOKEN` + explicit override (mirrors GitHub)
`config.py` after line 197:
```python
BITBUCKET_TOKEN_DEFAULT: Optional[str] = Field(
    default=None,
    validation_alias=AliasChoices("BITBUCKET_TOKEN_DEFAULT", "BITBUCKET_TOKEN"),
)
BITBUCKET_WORKSPACE: Optional[str] = None
GIT_PROVIDER: Optional[str] = None          # explicit override; else inferred from token
K8S_SECRET_KEY_BITBUCKET_DEFAULT: str = "bitbucket-token-default"
```
The `BITBUCKET_TOKEN` alias means the TUI/`.env` value flows in with no other
wiring — exactly how `GITHUB_TOKEN` → `GITHUB_TOKEN_DEFAULT` works today (199).

### 3. Platform secrets — hold the active provider's token
`github_platform_secrets.py`:
- Add `bitbucket_token_default: Optional[str]` and store the resolved
  `active_provider: GitProvider` on `GithubPlatformSecrets`.
- `_load_from_env` (116): read `settings.BITBUCKET_TOKEN_DEFAULT`; compute
  `active_provider = resolve_active_git_provider(settings)` (fails fast on
  ambiguous/unconfigured).
- `_build_secrets_from_map` (95) + `init_...` (132): read new K8s field
  `settings.K8S_SECRET_KEY_BITBUCKET_DEFAULT`; same active-provider resolution.
- Add `active_default_token` / `active_git_user_default` helpers returning the
  right values for the active provider.

### 4. Auth resolution — use the active provider
`github_auth.py` `resolve_github_auth_for_api_key_document` (28):
- **git_user_name default**: when not explicitly set on the key doc, use
  `strategy_for(secrets.active_provider).default_git_user` (BitBucket →
  `x-token-auth`) instead of the hardcoded `"x-access-token"` (line 47).
- **default-pool token** (58): use `secrets.active_default_token` instead of the
  hardcoded `github_token_default`; error message names the active provider.
- Per-key ciphertext path (42) is unchanged (opaque token); only its git_user
  default becomes provider-derived. No signature change needed since the active
  provider is global (read from `secrets`).

### 5. Token sanitization — apply all providers (load-bearing safety)
`workspace_pool.py:157` `_sanitize_token_in_message`: keep the explicit `token`
replacement (line 179, host-agnostic, already covers BitBucket), then loop over
`all_strategies()` applying each provider's patterns instead of the three
hardcoded GitHub `re.sub` calls (183–199). Apply **all** providers' patterns
(cheap; defends even if a BitBucket token ever appears in a GitHub deployment's
logs). BitBucket patterns: `@bitbucket\.org` host anchor, `x-token-auth:`
prefix, `ATCTT[A-Za-z0-9_=\-]+` token shape.

---

## Phase B — auto-provisioning (BitBucket Cloud REST)

`backend/scripts/create_generation_session_repos.py`:

- Add a `BitbucketCloudAPIClient` (parallel to the existing `GitHubAPIClient` at
  `:102`), base `https://api.bitbucket.org/2.0`, header
  `Authorization: Bearer {access_token}`:
  - `create_repository(workspace, repo_slug)` → `POST /repositories/{workspace}/{repo_slug}`
    body `{"scm": "git", "is_private": true}`; treat a 400 "already exists" as
    idempotent (mirrors GitHub `repository_exists` guard).
  - `repository_exists` → `GET /repositories/{workspace}/{repo_slug}` (200 = exists).
  - **No team/access-grant step** — the access token's owner already has write
    to repos it creates in the workspace (unlike the GitHub `--team` grant).
- Add `--git-provider {github,bitbucket_cloud}` (default: auto from configured
  token via `resolve_active_git_provider`; error if ambiguous) and
  `--bitbucket-workspace` (env `BITBUCKET_WORKSPACE`).
- Build `repo_url` from the provider instead of the hardcoded github.com at
  707/776: `https://bitbucket.org/{workspace}/{repo_slug}` for BitBucket. Emit it
  into the workspace doc via the existing `create_workspace_document` path (629)
  and the `--output-workspace-config` path.
- **P10Y metrics-enable is skipped for BitBucket** (that step hits GitHub-side
  registration). `p10y_repository_id` is set externally when Compass registers
  the BitBucket repo — treat BitBucket provisioning like the existing
  `--skip-metrics` path and let `p10y_repository_id` come from config (or null).

---

## TUI / onboarding

`mcp_server/tui/config.py`: add `BITBUCKET_TOKEN`, `BITBUCKET_WORKSPACE` to
`ENV_SECRET_KEYS` (32) and `BITBUCKET_TOKEN` to `MASKED_KEYS` (43). Add matching
entries to `.env.quickstart.example`. The load-time parity assertion stays green.
`mcp_server/tui/onboarding.py`: present GitHub **or** BitBucket as the git step
(a provider choice, honoring the no-mixing assumption), collecting either
`GITHUB_TOKEN`/`GITHUB_ORG` or `BITBUCKET_TOKEN`/`BITBUCKET_WORKSPACE`. BitBucket
wording: "paste a BitBucket Cloud access token — no username needed."

## P10Y — no code change
Confirmed neutral. Optionally add `BITBUCKET = "bitbucket"` to the dead `GitType`
enum for future clarity (no behavior change). Compass's BitBucket connection is a
user setup step (Compass supports it natively) — note it in the docs/PR
description as a prerequisite, just like the GitHub connection is today.

## Persistence / back-compat (zero migration)
- No per-workspace `git_provider` field is needed — provider is global. Existing
  workspace/api-key docs are read unchanged.
- All new `Settings`/secret fields default to `None`; a GitHub-only deployment
  (only `GITHUB_TOKEN` set) resolves to GitHub and behaves identically.
- API-key doc unchanged (still `github_token_ciphertext`); the ciphertext is an
  opaque BitBucket token in a BitBucket deployment.

## Explicitly out of scope (later PRs)
- CI/CD (Pipelines vs Actions), deploy/E2E — `gh` CLI, `GH_TOKEN`,
  `github_cli_env_for_generation`, deploy prompt recipes. Riskiest; last.
- BitBucket **Server/Data Center** (clone path `/scm/…`, arbitrary host, no
  managed CI).
- Full `git_providers/` package split (hosting/provisioning/deploy ABCs) and the
  `github_*`→`git_*` module/field renames + `PUT /auth/git-token` route alias.
- **Per-key / mixed-provider** support — explicitly excluded by the no-mixing
  assumption.
- Notification web-URL scheme (`/tree/` vs `/src/`) — cosmetic; can ride along.

## Tests
- Golden: GitHub sanitization + auth resolution byte-identical to current.
- `resolve_active_git_provider`: only-GitHub → GITHUB; only-BitBucket → CLOUD;
  both without `GIT_PROVIDER` → error; neither → error; explicit `GIT_PROVIDER`
  wins.
- Sanitizer: `ATCTT…`, `x-token-auth:@bitbucket.org` URL, explicit-token all
  scrubbed; GitHub cases unchanged.
- Auth: default-pool with active=BitBucket yields git_user `x-token-auth` + the
  BitBucket token; active=GitHub unchanged.
- Clone flow with active=BitBucket (mock `run_git`): authenticated URL is
  `https://x-token-auth:{token}@bitbucket.org/...`.
- Provisioning: `BitbucketCloudAPIClient` create/exists with mocked `httpx`;
  `--git-provider bitbucket_cloud --dry-run` emits a bitbucket.org `repo_url`;
  idempotent create on 400-already-exists.
- `mcp_server` config parity assertion green with the new keys.

Run `make unit-tests` (record baseline first; pass count ≥ baseline). Do **not**
use `make integration-tests`. `make check-complexity-diff` after (touches
`workspace_pool.py` and the provisioning script).

## End-to-end sanity
Set `BITBUCKET_TOKEN` + `BITBUCKET_WORKSPACE` (leave GitHub vars unset), run
`create_generation_session_repos.py --git-provider bitbucket_cloud`, confirm
repos created on bitbucket.org and workspace docs carry bitbucket URLs. Run a
generation: clone → agents commit → `git push origin main` succeeds; P10Y's
local `git log` allowlist picks up the SHAs. Verify a GitHub-only deployment is
byte-identical (golden tests green).
