# Repository guidance

Rules for anyone, person or coding agent, who changes this repository.

## Repository layout

- `backend/` - Python service (uv project): `src/`, `tests/`, `examples/`, `migrations/`.
  Rules: `backend/AGENTS.md`.
- `frontend/` - Next.js web app. Rules: `frontend/AGENTS.md`.
- `validation/` - the static checks that CI runs (`make validate`).
- `docs/` - configuration reference, graph design, design system.
- Repo root - `docker-compose.yml`, `.env.example`, `config/` and the `Makefile` that runs
  everything.

## Area rules

Before you read or change anything under `backend/`, read `backend/AGENTS.md`.
Before you read or change anything under `frontend/`, read `frontend/AGENTS.md`.
This file applies everywhere; an area file adds the rules for its code.

## Check a change

Run these from the repo root before you open a pull request:

| Command | What it checks |
|---|---|
| `make check` | Backend lint and offline tests |
| `make validate` | All static validation tracks (the same as CI) |
| `make frontend-build` | The frontend builds |
| `make e2e-production` | Offline browser tests against the built frontend |

## Frontend design system

UI work follows the design system in `docs/design-system/`. Start with its `USAGE.md`.
The tokens are the source of truth; do not edit that folder to make a screen fit.

## Never commit

- Secrets, API keys or tokens.
- `.env.local` or any other env file with real values.
- Screenshots or other QA images.
