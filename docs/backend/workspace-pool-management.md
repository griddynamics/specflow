# Workspace Pool Management

Operator-facing management of the workspace pool: seeing what exists, growing it, and getting
workspaces back. Surfaced in the TUI as the **workspaces screen** (`w` from the sessions
overview) and available directly over the API.

For the lifecycle rules these operations must respect — who may release an ALLOCATED
workspace, when archive is mandatory — see the STEEL COMMANDMENTS in `CLAUDE.md` and
`app/state/transitions.py`.

---

## The model in one paragraph

A workspace is two things kept in step: a **row** in the `workspaces` collection (identity,
status, lock owner) and a **git repository** on the NFS/Filestore volume at
`WORKSPACE_BASE_PATH/{workspace_id}`, cloned from a dedicated per-workspace GitHub remote.
Workspaces are grouped into **sets of `WORKSPACES_PER_SET` (3)** — the unit of allocation, so
the parallel variants of one generation are isolated per repo. Set membership and naming
(`ws-{set:02d}-{index}`, repos `{prefix}{n}`) are conventions owned by
`app/services/workspace_pool_seeding.py`.

The pool is **fixed and pre-seeded**: allocation never creates a workspace, it only claims an
existing available one. Growing the pool is therefore an explicit operator action.

---

## Endpoints

All under `/api/v1/workspace`, all `require_admin` (the explicit `"admin"` role — wildcard
permissions are rejected at key creation).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/pool/status` | Aggregate counters (total/available/allocated/cleaning/stuck, available sets, cleaning grace) |
| `GET` | `/pool/sets` | **Per-set listing** with each workspace's repo URL, lock owner, and reclaim verdict |
| `POST` | `/pool/reclaim` | Return workspaces or whole sets to AVAILABLE |
| `POST` | `/pool/expand` | Add N sets: create repos, register with P10Y, seed (202 + job id) |
| `GET` | `/pool/expand/{job_id}` | Progress of an expansion |
| `POST` | `/pool/shrink` | Remove slots from the pool; **GitHub repos are kept** |

`/pool/status` remains the cheap capacity check used by the run/clear flows; `/pool/sets` is
the management view. Both derive the CLEANING grace countdown from the same helper
(`workspace_pool.remaining_grace_seconds`) so they cannot disagree.

Listings and mutations are scoped to the caller's own `workspace_pool`
(`request.state.workspace_pool`).

---

## Listing — `GET /pool/sets`

`WorkspacePoolService.list_pool_sets` groups every workspace by `(workspace_pool,
set_number)`, and for ALLOCATED members joins the owning generation (once per generation, not
once per workspace) to report `owner_generation_status` and `owner_code_archived`.

Per set it reports **`allocatable`** and, when false, a **`blocked_reason` that names the
offending member** — computed with the same predicate allocation itself uses (exactly 3
members, all AVAILABLE with `clean_verified is True`). This is what answers "why does my run
say no workspaces are available?" without reading logs.

Per workspace it reports the **reclaim verdict** from `classify_reclaim` — see below.

---

## Reclaim — `POST /pool/reclaim`

The operator intent is always "give me this workspace back"; which primitive achieves that
depends on where the workspace currently sits. One classifier,
`app/schemas/workspace_pool_management.classify_reclaim`, makes that decision, and **both** the
listing badge and the reclaim dispatch read it — so what the UI offers and what the server does
cannot drift.

| Current state | `reclaim_action` | What runs |
|---|---|---|
| CLEANING | `finish_cleaning` | `cleanup_workspace` |
| AVAILABLE, not `clean_verified` | `force_clean` | `force_clean_available_workspace` |
| STUCK | `release_stuck` | `force_release_stuck_workspace` (first HTTP exposure) |
| ALLOCATED, owner terminal or absent | `release_and_clean` | `force_release` → `cleanup_workspace` |
| ALLOCATED, owner still live | `blocked` | nothing — refused, with the generation named |
| AVAILABLE and clean | `already_available` | nothing — reported as a no-op |

"Owner terminal" means the generation named by `locked_by` is `COMPLETED`, `FAILED`, or
`CANCELLED` — `GenerationStatus.terminal()`, the same definition the state machine and the
7-day wipe job use. An **unrecognised** status is treated as *not* terminal, so an unknown
value can never read as "safe to reclaim".

**Reclaiming does not destroy generated code.** `cleanup_workspace` commits the working tree to
branch `{generation_id}`, pushes it, and **verifies the push via `git ls-remote`** before
wiping; a failed verification sends the workspace to STUCK rather than continuing. What is lost
when reclaiming a FAILED-but-unarchived run is the ability to *retry in place* — flagged as
`retry_lost_on_reclaim` so the UI can warn before confirming.

Every outcome is per-workspace: the endpoint always returns 200 and a `details` list, so one
refused member neither hides the successes nor aborts the batch. `force_release` writes its
audit trail (`force_release_reason`, `force_released_by`) with `confirmed_by` taken from the
**authenticated caller**, not from the request body.

This replaced a client-side loop that rebuilt the three member ids from the naming convention
and fired three separate requests. Set membership is now read from the database, so a set with
unexpected membership is handled correctly instead of addressing ids that may not exist.

---

## Expansion — `POST /pool/expand`

Returns **202 with a `job_id`**; the work continues in the background. It has to: P10Y/Compass
has **no create-repository API**. A repository row appears in Compass only when it ingests the
GitHub *connection* that owns the repo, so the sequence is inherently eventual:

```
create on GitHub  →  sync_repositories(connection)  →  poll until git_url appears  (~60s)
                  →  enable_metrics(ids)            →  poll until ready            (up to 5 min)
                  →  seed workspace rows
```

Phases are reported as `queued → creating_repos → awaiting_p10y → enabling_metrics → seeding →
done | failed`, with a human-readable `messages` log the TUI streams into its log pane.

**Naming continues the pool's own convention.** `derive_naming_scheme` infers `(github_org,
prefix)` and the highest repo/set numbers from the existing `repo_url` values, using the most
common pair so one hand-added oddity cannot redirect where new repos go. This is deliberate: the
provisioning script's default prefix (`generation-workspace`) and `WORKSPACE_REPO_PREFIX`
(`specflow-workspace`) disagree, so trusting a configured default would create a second,
differently-named family of repos beside the first. A pool of 3 sets (repos 1–9) expanded by 3
yields `ws-04-*`…`ws-06-*` backed by repos 10–18, with nothing renumbered.

Two invariants worth stating outright:

- **Seeding happens last.** A workspace row is written only once its repo exists on GitHub *and*
  has a P10Y id, so the pool never advertises a slot that allocation could not clone or
  estimation could not measure.
- **`replace=False`.** Expansion never rewrites an existing workspace document. The bootstrap
  script seeds with `replace=True`, which would reset a live document to
  `available`/`locked_by=None` — running that as "expansion" would drop an in-flight
  allocation.

Job state is **in-memory** (`PoolExpansionRegistry` on `app.state`, mirroring
`GenerationTaskRegistry`). That is sufficient because every step is idempotent: GitHub creation
skips existing repos, P10Y discovery matches on `git_url`, seeding skips existing ids. A restart
mid-expansion loses the progress record (the job endpoint 404s and says so); re-running
completes whatever is outstanding. One expansion at a time per pool — two concurrent runs would
derive the same "next free" numbers and collide. `MAX_SETS_PER_EXPANSION` (20) stops a typo
creating hundreds of repositories.

Requires `GITHUB_TOKEN` (repo creation) and `P10Y_BASE_URL` / `P10Y_API_KEY` /
`P10Y_ORGANISATION_ID`; missing configuration is a 400 naming the unset variables, not a 500.

---

## Shrink — `POST /pool/shrink`

Removes workspace rows so the sets stop being allocatable. **GitHub repositories are not
deleted.** Each workspace repo holds the `archive/{generation_id}` branches of every run that
ever used it — the only remote copy of that code (Steel Commandment I) — so deleting repos would
destroy generation history. Expansion can re-adopt the same repos afterwards.

Only **AVAILABLE and `clean_verified`** workspaces are removed. Anything allocated, cleaning, or
stuck is refused with an instruction to reclaim it first, so shrinking can never strand work.

---

## Seeding a pool from scratch

Expansion grows an existing pool; the initial pool comes from the host-side scripts, which share
the same provisioning services:

```bash
# create repos + register with P10Y + seed
python backend/scripts/create_generation_session_repos.py \
    --start 1 --end 9 --prefix specflow-workspace --github-org my-org

# or seed from a config file of pre-existing repos
python backend/scripts/init_db.py --workspace-config e2e-workspace-config.json --yes
```

`specflow init --max-parallel-runs K` wraps the first form (K sets of 3).

`GitHubAPIClient` / `provision_repositories`
(`app/services/github_repo_provisioner.py`) and the P10Y discovery dance
(`app/services/p10y_repository_discovery.py`) live in `app/services` precisely so the script and
the endpoint drive one implementation.

---

## TUI

`w` from the sessions overview opens the workspaces screen (app-wide controls live on
`SessionsScreen`, not the per-generation dashboard).

| Key | Action |
|---|---|
| `↑`/`↓` | select a set header or a workspace |
| `o` | open the highlighted workspace's GitHub repo |
| `c` | reclaim the highlighted workspace, or the whole set from its header |
| `x` | add sets (pick a count; progress streams into the log pane) |
| `d` | remove the highlighted set from the pool |
| `r` | reload |
| `esc` | back |

Destructive actions confirm with a 10-second countdown, and the confirmation states what
actually happens — that work is archived and pushed before a wipe, and that shrink keeps the
GitHub repositories. All row/message formatting is pure functions in `mcp_server/tui/render.py`
so it is unit-tested without a running app.

---

## Key files

| Path | Role |
|---|---|
| `app/api/v1/workspaces.py` | Endpoints + request/response models |
| `app/services/workspace_pool.py` | `list_pool_sets`, `reclaim_workspace`, allocation, cleanup |
| `app/schemas/workspace_pool_management.py` | `classify_reclaim` — the shared reclaim rule |
| `app/services/workspace_pool_expansion.py` | Expansion orchestration, job registry, shrink |
| `app/services/github_repo_provisioner.py` | GitHub repo creation |
| `app/services/p10y_repository_discovery.py` | Compass re-fetch, id discovery, metrics |
| `app/services/workspace_pool_seeding.py` | Document shape, naming scheme, idempotent upsert |
| `app/core/workspace_pool_names.py` | Pool slugs, `WORKSPACES_PER_SET` |
| `mcp_server/tui/app.py` | `WorkspacesScreen`, `SetCountScreen` |
| `mcp_server/tui/render.py` | Pure row/message formatters |
| `mcp_server/services/cli_service.py` | HTTP client functions |
