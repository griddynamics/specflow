# Frontend (SpecFlow)

Next.js 16 (App Router, Turbopack) with Auth.js v5 (`next-auth` beta), a
backend-for-frontend (BFF) route that talks to the API, the GD Liquid Glass design
system, and Playwright for end-to-end tests. Read `AGENTS.md` first: this Next.js has breaking changes, and
its docs are in `node_modules/next/dist/docs/01-app/`. Read the relevant guide
before changing code.

## Run

From the repo root (the stack, the normal way):

```shell
make env-local                   # writes .env.local: random local secrets, then asks for your LLM settings
make up                          # the stack; the frontend container (the image Infra builds) on http://127.0.0.1:3001
```

`make up` starts no Temporal and no Langfuse: it uses your own (`TEMPORAL_ADDRESS`, default
`host.docker.internal:7233`). `make up WITH=temporal,langfuse` adds the bundled ones
(see `docs/configuration.md`).

For live reload, stop the frontend container and run `next dev` on the host (both use `FRONTEND_PORT`):

```shell
docker stop specflow-frontend
make frontend-setup frontend-dev   # npm ci, then next dev on 127.0.0.1:3001
```

The container and the host target both get their values from `.env.local` in the repo root; `scripts/check-env.mjs` checks the
environment before `dev`, `start` and the runtime `server.js`, and exits 2 with one
JSON line that names the bad variables (never their values).

`make frontend-dev` loads the repo-root `.env.local` with Node's `--env-file` parser
(Node 20.6 or newer; `ENV_FILE=` overrides the path); variables already set in your shell
win over the file. It stops with a message if the file is missing or if `npm` is not a node
script (a Volta, asdf or mise shim); then source the file in your shell and run
`npm --prefix frontend run dev` yourself.

## Routes

Every screen is built once (`components/<area>/`); `lib/routes.ts` is the only place that knows a path.

| Path                                                                 | Tree                    | What it shows                                                                                 |
| -------------------------------------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------- |
| `/login`                                                             | none (no shell)         | Sign-in on the design system: dev e-mail form (`AUTH_MODE=dev`) or one OIDC button            |
| `/`                                                                  | production `app/(app)/` | Opens the newest project's Mission Control; with no project an empty state with "New project" |
| `/projects/<id>/mission-control`                                     | production              | Mission Control on the real backend (upload, parse, summarize through the BFF)                |
| `/projects/<id>/decisions`, `/projects/<id>/specbot`, `/connections` | production              | Placeholder screens: no endpoint yet, so they show an empty state                             |
| `/projects/<id>`, `/projects/<id>/documents/<doc>`                   | old hello routes        | 307 redirect to `/projects/<id>/mission-control`                                              |

Production routes need a session (the proxy redirects to `/login`; `app/(app)/layout.tsx` checks again). The production
tree uses the real backend for `/me`, the project list, "New" (name only) and Mission Control; the other screens have no
backend endpoint yet. An unknown or foreign project id redirects to `/`. The avatar menu shows name and e-mail from `GET /me` and
"Sign out". A 401 from the BFF signs the user out and shows `/login`.

Theme: `<html data-theme>` from the cookie `specflow-theme` (`light` or `dark`); light by default, also when the OS
prefers dark. The design-system CSS is imported once by `app/layout.tsx`; `app/globals.css` holds only the base rules.

## Test, lint, build

Run these in `frontend/` (or use `make frontend-lint` / `make frontend-build` from the root):

| Command                     | What it covers                                                                                                                                                                                                                                                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `npm run lint`              | ESLint, including `e2e/**`, `tests/**`, `scripts/**`                                                                                                                                                                                                                                             |
| `npx tsc --noEmit -p .`     | type check. On a fresh clone run `npm run build` (or `npx next typegen`) first: the route types it needs are generated into `.next/types`                                                                                                                                                        |
| `npm run build`             | production build and type check (also checks `e2e/**`)                                                                                                                                                                                                                                           |
| `npm test`                  | unit and UI tests (`tests/unit`, `tests/ui`; the UI tests start Chromium against the built app and the stub API on the production routes, so run `npm run build` first)                                                                                                                          |
| `npm run test:oidc`         | OIDC contract tests against a local stub issuer (login, refresh single-flight, PKCE, `PUBLIC_URL`)                                                                                                                                                                                               |
| `npm run test:upload`       | streams a generated 200 MiB body through the BFF against a stub API and checks memory (gate 128 MiB)                                                                                                                                                                                             |
| `npm run e2e` or `make e2e` | Playwright against the running stack, real model calls (see below)                                                                                                                                                                                                                               |
| `npm run test:production`   | production data adapter (`lib/data/http`) against the stub API through the real BFF (`tests/production/*.test.ts`, ports 3250-3257). Run `npm run build` first: without `.next/standalone/server.js` the tests stop with "run `npm run build` first". No backend, no model calls                 |
| `npm run e2e:production`    | Playwright on the production routes (`e2e-production/`) against the stub API: starts the built app on port 3260 in dev sign-in mode with `scripts/serve-production.mjs` (stub API 3261, test control 3262), so run `npm run build` first. Both viewports, no retries. No backend, no model calls |
| `npm run check:tokens`      | all app UI code (`app/**`, `components/**`, `lib/**/*.tsx`, `styles/**` except `styles/ds`) uses design tokens only (no literal colours, font sizes, families, radii, shadows, blur). It does NOT check spacing, sizes, `gap`, `padding` or `font-weight` literals                               |

Install the browser once: `npx --prefix frontend playwright install chromium`.

### End-to-end (Playwright)

`make e2e` runs `frontend/e2e/hello.spec.ts` and `edge.spec.ts` on the new screens:

- `hello.spec.ts`: dev sign-in, "New" project (name only), upload the fixture PDF in Mission Control, parse and summarize
  run on their own until Ready, then the summary is read through the API (real document contents have no screen), the
  Langfuse trace is checked, and "Sign out" from the avatar menu.
- `edge.spec.ts`: duplicate upload and a 200 MiB upload through the BFF (API level), non-PDF through the API and in an Empty
  Mission Control ("Unsupported file type", Remove), and Mission Control on uploaded documents shows "Continue" and starts
  nothing.

They need the whole stack up (`make up WITH=temporal,langfuse`, or your own Temporal and Langfuse), and the hello spec spends real model calls (it never
retries). Use a cheap model for the run, set through the environment only, for example
`AGENT_MODEL=<haiku model id> make up` then `make e2e`, then `make down`. Base URL:
`E2E_BASE_URL` (default `http://localhost:3001`). Reports and screenshots go to `frontend/logs/e2e/` (git-ignored). Each
created project's storage objects are deleted by project prefix afterwards.

## Environment variable names

Only names here; see `.env.example` for comments and `docs/configuration.md`
for roles and secret flags.

- Always: `APP_ENV`, `AUTH_MODE` (`dev` or `oidc`), `PUBLIC_URL`, `API_INTERNAL_URL`, `SESSION_SECRET` (32 bytes or more).
- `AUTH_MODE=dev` (local only): `AUTH_DEV_SIGNING_KEY` (same key as the backend), `LOCAL_TEST_USER_EMAIL`.
- `AUTH_MODE=oidc`: `OIDC_ISSUER`, `OIDC_INTERNAL_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`. A dev signing key is refused here in every environment.
- Container only: `FRONTEND_PORT` (the host port); `PORT` and `HOSTNAME` are set by the image.
- No `NEXT_PUBLIC_*` variable is allowed (a test enforces it): everything is read at run time.

## Auth modes

- **dev** (local): the login page shows an e-mail form; the server signs an HS256 token with `AUTH_DEV_SIGNING_KEY`. Refused unless `APP_ENV=local` and not in Kubernetes. The server logs `auth.mode.dev_enabled` at start.
- **oidc** (the default; any OpenID Connect provider, for example Keycloak): Authorization Code with PKCE; the redirect URI is `<PUBLIC_URL>/api/auth/callback/oidc`. Refresh is single-flight per refresh token.

The browser holds only the session cookie (`specflow-session`, or `__Host-specflow-session` on https), never a token. Tokens are used only inside the BFF.

## BFF rules

All browser calls go to `/api/backend/*` (`app/api/backend/[...path]/route.ts`), which
adds the Bearer token and forwards to the API.

- The route proxies with `node:http`/`node:https` and pipes the request stream. **Never use `fetch` to proxy**: it buffers or times out on large streamed uploads (a static test bans `fetch(` in the route).
- Upload is the raw `File` as the body with `Content-Type: application/pdf` and `X-Requested-With: specflow`. No multipart, no size limit in the app.
- Mutations need a matching `Origin` and `X-Requested-With`; encoded path tricks (`%2f`, `%5c`, `%25`, `..`) get 400; cookies and forwarding headers are not passed on.
- `lib/gc-pacing.ts` is the memory mitigation for big uploads: while a body streams, it calls `gc()` every 8 MiB (best effort, no-op if unavailable). Without it, peak memory on a 200 MiB upload sometimes passed the 128 MiB gate on macOS (one run at +130 MiB). Keep it when touching the route.

## Test hooks

UI elements the tests use carry `data-testid`. Find them with `grep -rn data-testid components app`; do not rename one
without updating `tests/ui`, `e2e` and `e2e-production`.

| Where                | Hooks                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/login`             | `login-page`, `login-logo` (wrapper of the light/dark logo pair), `login-dev-email`, `login-dev-submit` (dev mode), `login-oidc` (OIDC mode); `login-error` (a failed sign-in, fixed message), `login-still-signed-in` (note when the session is still valid after a sign-out)                                                                                                                                                                                                                                                                                   |
| Production `/`       | `home-loading`, `home-empty`, `home-new` (opens the Create project modal), `home-error`                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Production tree      | `production-root` (the design-system root wrapper)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Shell                | `signing-out` (the busy state while a sign-out or a 401 ends the session), `app-shell`, `shell-main`, `shell-loading`, `shell-error`, `create-project-slot`; avatar menu "Sign out" by role `menuitem`                                                                                                                                                                                                                                                                                                                                                           |
| Create project modal | `create-project-modal`, `cp-create`, `cp-error`; the name field by its label "Name"                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Mission Control      | `screen-mission-control` (`data-phase` = `loading`, `empty`, `processing`, `attention`, `ready`), `mc-empty` (its `input[type=file]` takes the files), `mc-selected`, `mc-processing`, `mc-card-title`, `mc-file-row` (`data-state` = `active`/`done`/`failed`/`stuck`, or `ready` while only selected; `data-stage` = `upload`/`read`/`extract`/`done`), `mc-graph` (`data-state`), `mc-graph-step`, `mc-ready` (`data-view`), `view-readme`; row buttons by role and name: `Retry <file>`, `Continue <file>`, `Remove <file>`; "Create project" starts the run |
| Other screens        | `screen-decisions`, `screen-specbot`, `specbot-greeting`, `screen-connections` (`data-state`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

The hello hooks `project-name-input`, `project-create`, `project-row`, `upload-input`, `document-row`, `document-status`,
`parse-button`, `summarize-button`, `summarization-status`, `summary-text`, `summary-title`, `graph-node-title`,
`markdown-preview`, `stuck-hint`, `stuck-retry` and `logout` are gone with the hello pages.

## Known limits

- Project and document lists read the first 100 entries only (no "load more").
- Real parse and summarize results have no screen: the summary is visible through the API only.

## Design system and checks

Design-system files are copied unchanged from `docs/design-system/` into `styles/ds/` and `public/ds/` (do not edit; re-copy).
`/ds/**` (logos) and `/icon.svg` never need login. Light theme by default; dark through the cookie `specflow-theme=dark`.

- `check:tokens` scans `app/**`, `components/**`, `lib/**/*.tsx` and `styles/**` (not `styles/ds`). One exception per
  line: a trailing `// tokens-ok: <reason>`; the count is printed.
- `next build` may run while `next dev` serves (dev writes to `.next/dev`).

## Clone check config

`.jscpd.json` mirrors the kit `jscpd.json` and additionally ignores `styles/ds/tokens.css`: it is a byte-equal copy of the read-only design system tokens (enforced by `tests/unit/ds-tokens-parity.test.mjs`), so it cannot be deduplicated locally.
