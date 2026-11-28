# Repository guidance

Rules for anyone, person or coding agent, who changes this repository.

## Repository layout

- `backend/` - Python service (uv project): `src/`, `tests/`, `examples/`, `migrations/`.
  Rules: `backend/AGENTS.md`.
- `frontend/` - Next.js web app. Rules: `frontend/AGENTS.md`.
- `validation/` - the static checks that CI runs (`make validate`).
- `docs/` - configuration reference, graph design, file parsers.
- Repo root - `docker-compose.yml`, `.env.example`, `config/`, the `Makefile` that runs
  everything and `scripts/` (shell scripts the `Makefile` calls, such as `env-local.sh`).

## Area rules

Before you read or change anything under `backend/`, read `backend/AGENTS.md`.
Before you read or change anything under `frontend/`, read `frontend/AGENTS.md`.
This file applies everywhere; an area file adds the rules for its code.

## Check a change

Name branches `fix/<title>`, for example `fix/docling-pdf-parser`.

Run these from the repo root before you open a pull request:

| Command | What it checks |
|---|---|
| `make check` | Backend lint and offline tests |
| `make validate` | All static validation tracks (the same as CI) |
| `make frontend-build` | The frontend builds |
| `make e2e-production` | Offline browser tests against the built frontend |

## Pull requests and commits

- Do not mention internal tickets in a pull request title or description, or in a commit
  message. This includes Jira keys (for example `ABC-123`) and links to the internal Jira.
- Describe the change, its reason and how you checked it. A reader without access to the
  internal tracker must understand the pull request.

## Frontend design system

Every screen is Graphico: the UI kit in `packages/ui`, its source of truth
`docs/mission-control/landing.css` (decision D14 in `docs/mission-control/DECISIONS.md`). Start with
`frontend/README.md` and the kit's own docs; do not restyle a screen by hand.

## Never commit

- Secrets, API keys or tokens.
- `.env.local` or any other env file with real values.
- Screenshots or other QA images.
