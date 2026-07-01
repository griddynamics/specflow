# Tech Spec — Replace Firestore Emulator with SQLite in Docker dev stack

Status: Draft (awaiting review)
Branch: `feature/use-sqllite-instead-firestore`
Source: adapted from `griddynamics/gd-specflow` PR #314 ("Local quickstart: SQLite backend + no-Docker bare-mode"), scoped down.

## Scope

PR #314 upstream does two independent things: (1) a SQLite `IDatabase` backend, (2) a
`--bare-mode` no-Docker host runner built on top of it. We only want (1), applied to the
**existing Docker dev stack** — `docker-compose up` should run backend + Postgres-free,
Java-free SQLite instead of backend + `firestore-emulator` + `firestore-exporter`.

**In scope:**
- New `SqliteDatabase` (`IDatabase` implementation), wired through `factory.py`.
- `docker-compose.yml` dev profile: drop `firestore-emulator` / `firestore-exporter`
  services; backend gets `DATABASE_TYPE=sqlite` + a volume-mounted db file.
- Seeding script generalization (`init_firestore.py` → `init_db.py`) so `make init-db`
  works against whichever backend is active.
- Startup validation treating `sqlite` as a non-critical-pool backend, same as `emulator`/`memory`.
- Test suite: shared `IDatabase` contract extracted once, run against both memory and sqlite.

**Out of scope (stays as upstream's follow-on, not ported here):**
- `specflow-init.sh --bare-mode` / host-runtime detection.
- `scripts/stage-rosetta-plugin.sh`.
- Any "run without Docker at all" documentation or tooling.
- Production Firestore path — untouched; `sqlite` is dev/local only, single-writer,
  no cross-node locking (matches [[STEEL COMMANDMENT XI scope note]] — this doesn't touch
  coding/deploy archival guarantees, it only changes the *local dev* persistence layer).

## Design (ported as-is from PR #314, verified against current repo state)

### 1. `backend/app/database/sqlite.py` (new file, ~440 lines)
Self-contained `IDatabase` implementation, no dependency on bare-mode code:
- Two JSON-blob tables (`documents`, `subdocuments`), primary-keyed on
  `(collection, doc_id)` / `(parent_collection, parent_doc_id, subcollection, doc_id)`.
- Filters pushed into SQL via `json_extract` (`==`, `!=`, `<`/`<=`/`>`/`>=`, `in`, `array_contains`).
- `run_transaction()` uses real `BEGIN IMMEDIATE` with busy-retry on `sqlite3.OperationalError`
  ("database is locked").
- Datetime contract: every datetime stored as fixed-width ISO-8601 UTC text (`_canonical_dt`),
  decoded back to tz-aware `datetime` on read (`_decode_from_storage`) — required so
  `stuck_running_detector` / lease-recovery comparisons against `datetime.now(UTC)` behave
  identically to Firestore.
- WAL mode + `busy_timeout`; single-writer by design (this is the local/dev story, not a
  distributed-locking replacement for prod Firestore).
- Ported verbatim — no adaptation needed, it has no bare-mode coupling.

### 2. Wiring
- `backend/app/core/enums.py`: add `SQLITE = "sqlite"` to `DatabaseType`.
- `backend/app/core/config.py`: add `SQLITE_DB_PATH: str = "./.specflow-local/specflow.db"`
  — comment carries over the NFS warning (SQLite file locking is unsafe over NFS; must be
  block storage). For the Docker case this becomes a named Docker volume, not a bind mount
  to NFS-backed `WORKSPACE_MOUNT_PATH`.
- `backend/app/database/factory.py`: add `elif db_type == DatabaseType.SQLITE:` branch
  constructing `SqliteDatabase(db_path=settings.SQLITE_DB_PATH)`; extend
  `clear_test_data()`'s allowed-types tuple to include `SQLITE`.
- `backend/app/services/startup_validation.py`: extend the existing
  `database_type in ("emulator", "memory")` non-critical checks to
  `("emulator", "memory", "sqlite")` (workspace-pool + filestore warnings, not fatal) and
  the k8s-readiness branch's `(FIRESTORE, EMULATOR)` tuple to include `SQLITE`.

### 3. Seeding: `init_firestore.py` → `init_db.py`
Rename + generalize (upstream's version, minus the bare-mode-only env-precheck removal
rationale — we keep that part too since it's a genuine simplification): drop the
Firestore-only env prechecks from `main()` since misconfiguration now fails fast inside
`get_database()` itself (single source of truth, per this repo's "no duplicate validators"
convention). Update all doc/error-string references in `local_identity.py` and
`local_auth.py` (comment-only changes, no behavior change).

### 4. `docker-compose.yml` (not touched by upstream PR #314 — new for this scoped port)
Current dev profile: `firestore-emulator` + `firestore-exporter` sidecar, backend depends on
emulator health check, `DATABASE_TYPE=emulator`.

New dev profile:
- Remove `firestore-emulator` and `firestore-exporter` services (and their `profiles: ["", "dev"]`
  entries) — or gate them behind a `firestore-emulator` profile so `--profile test` (real GCP
  Firestore path) and anyone who wants to force the emulator can still opt in explicitly.
- Backend service: `DATABASE_TYPE=sqlite`, `SQLITE_DB_PATH=/data/specflow.db` (container path),
  new named volume `sqlite-data:/data`. Drop `depends_on: firestore-emulator`.
- `make init-db` / `make e2e-setup`: seeding no longer needs `FIRESTORE_EMULATOR_HOST` —
  runs `DATABASE_TYPE=sqlite uv run scripts/init_db.py $(INIT_DB_ARGS)` inside the backend
  container (or via `docker compose exec backend ...`, matching how `init-firestore` currently
  execs against the running stack — needs confirmation, see open question below).
- `--profile test` (real Firestore) path is untouched.

### 5. Tests
- Extract `backend/test/database/db_contract.py`: shared `IDatabase` contract test classes
  (`TestBasicCRUD`, `TestQuery`, `TestTransactions`, `TestArrayOperations`, `TestIsolation`,
  `TestServerTimestamp`, `TestApiKeyByUid`), parametrized by a `db` fixture. `test_memory_db.py`
  shrinks to import + run the shared contract against `InMemoryDatabase`.
- `backend/test/database/test_sqlite_db.py`: runs the same shared contract against
  `SqliteDatabase(":memory:")`, plus SQLite-only datetime-boundary tests (tz-aware roundtrip,
  naive-assumed-UTC, `<` filter excludes `None`, nested datetime-in-list roundtrip) and a
  persistence test (`state_survives_reopen` — write via one connection, read via a fresh one)
  and an end-to-end `detect_stuck_running` test against the real `StateMachineDBAdapter`.
- `test_factory.py`: add `test_factory_returns_sqlite_database`.
- `test_startup_validation.py`: add `test_sqlite_empty_pool_is_non_critical` regression test.
- `test_init_db.py` (renamed from `test_init_firestore.py`): update references.

## Open questions (need your call before implementation)

1. **Docker volume path for the SQLite file** — a named Docker volume (`sqlite-data:/data`,
   survives `docker compose down` but not `down -v`) vs. a bind mount under
   `${WORKSPACE_MOUNT_PATH}` like the current emulator export dir. Given the NFS-unsafe
   warning in `config.py`'s upstream comment, and that `WORKSPACE_MOUNT_PATH` is Filestore/NFS
   in this repo's architecture, a **named volume is the safe default** — recommend that unless
   you want the db file inspectable on the host.
2. **Keep the Firestore-emulator path as an opt-in profile**, or delete it outright? Recommend
   keeping it behind a profile (e.g. `--profile firestore-emulator`) for anyone still testing
   against real Firestore semantics locally, rather than a hard rip-out — cheap to keep, and
   avoids a one-way door.
3. **`make init-db` execution context** — currently `init-firestore` runs from the host via
   `uv run` against `FIRESTORE_EMULATOR_HOST=localhost:8080` (the emulator's exposed port).
   SQLite has no exposed port — seeding must run either (a) inside the backend container via
   `docker compose exec`, or (b) from the host directly against the same file if the volume is
   also bind-mounted to a host path. Recommend (a) for consistency with "the file lives in the
   container's volume, nothing outside touches it directly."
4. Do you want the upstream rename `init_firestore.py` → `init_db.py` (touches Makefile,
   local_auth.py comments, local_identity.py comments, error strings), or keep the filename and
   just add a sqlite branch inside it? Recommend the rename — matches upstream and this repo's
   "single source of truth, no special-casing" convention — but it's a wider diff (touches
   4-5 files for renames alone) if you want to minimize footprint instead.

## File list (estimated)

| File | Change |
|---|---|
| `backend/app/database/sqlite.py` | new (~440 lines, ported verbatim) |
| `backend/app/core/enums.py` | +1 line |
| `backend/app/core/config.py` | +3 lines |
| `backend/app/database/factory.py` | ~+10/-4 |
| `backend/app/services/startup_validation.py` | ~+9/-10 |
| `backend/scripts/init_firestore.py` → `init_db.py` | rename + ~-13 lines |
| `backend/app/core/local_identity.py` | docstring ref only |
| `backend/app/middleware/local_auth.py` | doc/error-string refs only |
| `docker-compose.yml` | dev profile: swap emulator services for sqlite volume (new design, not from upstream) |
| `Makefile` | rename `init-firestore*` → `init-db*`, drop `FIRESTORE_EMULATOR_HOST` requirement in the sqlite path |
| `.env.example` / `.env.quickstart.example` (whichever this repo uses) | `DATABASE_TYPE` default, `SQLITE_DB_PATH` |
| `backend/test/database/db_contract.py` | new (shared contract, extracted from `test_memory_db.py`) |
| `backend/test/database/test_memory_db.py` | shrinks to contract-runner |
| `backend/test/database/test_sqlite_db.py` | new |
| `backend/test/database/test_factory.py` | +1 test |
| `backend/test/services/test_startup_validation.py` | +1 test |
| `backend/test/scripts/test_init_firestore.py` → `test_init_db.py` | rename + ref updates |

Not ported: `specflow-init.sh`, `scripts/stage-rosetta-plugin.sh`, `QUICKSTART.md` bare-mode
sections, `agents/IMPLEMENTATION.md` (upstream changelog entry, not applicable here).
