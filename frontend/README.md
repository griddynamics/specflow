# Frontend (SpecFlow)

Next.js 16 (App Router, Turbopack) with Auth.js v5 (`next-auth` beta), a
backend-for-frontend (BFF) route that talks to the API, the Graphico UI kit
(`packages/ui`), and Playwright for end-to-end tests. Read `AGENTS.md` first: this Next.js has breaking changes, and
its docs are in `node_modules/next/dist/docs/01-app/`. Read the relevant guide
before changing code.

## Run

From the repo root (the stack, the normal way):

```shell
make env-local                   # writes .env.local: random local secrets, then asks for your sign-in email and LLM settings
make up                          # the stack, with the local Keycloak; the frontend container (the image Infra builds) on http://localhost:3001
```

Sign in at `http://localhost:3001` (not `127.0.0.1`: the sign-in returns to `localhost`) with the email you gave
`make env-local` and `LOCAL_TEST_USER_PASSWORD` from `.env.local`. Use Chromium or Firefox: Safari rejects the `Secure`
cookies of the local Keycloak on `http://localhost`. See `QUICKSTART.md`.

`make up` starts no Temporal and no Langfuse: it uses your own (`TEMPORAL_ADDRESS`, default
`host.docker.internal:7233`). `make up WITH=temporal,langfuse` adds the bundled ones
(see `docs/configuration.md`).

For live reload, stop the frontend container and run `next dev` on the host (both use `FRONTEND_PORT`):

```shell
docker stop specflow-frontend
make frontend-setup frontend-dev   # npm ci, then next dev on 127.0.0.1:3001
```

The container gets its sign-in values from `docker-compose.yml`; the host target gets all of its values from `.env.local` in
the repo root, so set `APP_ENV`, `PUBLIC_URL`, `API_INTERNAL_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` there
(values: [`docs/configuration.md`](../docs/configuration.md#web-app-on-the-host)). `scripts/check-env.mjs` checks the
environment before `dev`, `start` and the runtime `server.js`, and exits 2 with one
JSON line that names the bad variables (never their values).

`make frontend-dev` loads the repo-root `.env.local` with Node's `--env-file` parser
(Node 20.6 or newer; `ENV_FILE=` overrides the path); variables already set in your shell
win over the file. It stops with a message if the file is missing or if `npm` is not a node
script (a Volta, asdf or mise shim); then source the file in your shell and run
`npm --prefix frontend run dev` yourself.

## Routes

The V2 screens are the app: routes under `app/(v2)/`, components under `components/v2/`. `lib/routes.ts` is the only
place that knows a path.

| Path                          | Tree                                                   | What it shows                                                                                         |
| ----------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `/login`                      | none (no shell)                                        | Starts the OIDC sign-in at once; a small Graphico card only for a failed sign-in or a local sign-out  |
| `/`                           | `app/(v2)/(session)`                                   | Opens the newest project's canvas; with no project the uncloseable "Create your first project" window |
| `/projects/<id>`              | `app/(v2)/(session)/projects/[projectId]`              | The canvas: hubs and wires, the gaps dialog, the upload and integration forms                         |
| `/projects/<id>/integrations` | `app/(v2)/(session)/projects/[projectId]/integrations` | The Integrations page: a card per app and the MCP setup commands; the OAuth returns land here         |

Every page needs a session (the proxy redirects to `/login`; the `(session)` layout checks again with `requireUser`). A
project the caller cannot see answers 404 (the project gate of the `[projectId]` layout), and so do the removed legacy
URLs (`/projects/<id>/mission-control`, `/projects/<id>/gaps`, `/projects/<id>/specbot`, `/connections`, `/guides/...`).
The V2 header holds the project menu, Members and Integrations; the avatar menu shows name and e-mail from `GET /me` and
"Sign out". A 401 from the BFF signs the user out and shows `/login`.

Theme: `<html data-theme>` from the cookie `specflow-theme` (`light` or `dark`); light by default, also when the OS
prefers dark. The Graphico CSS (`@graphico/ui/styles.css`, every rule under `.gx`) is imported once by `app/layout.tsx`;
`app/globals.css` holds only the base rules.

## Test, lint, build

Run these in `frontend/` (or use `make frontend-lint` / `make frontend-build` from the root):

| Command                     | What it covers                                                                                                                                                                                                                                                                                                                                                               |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm run lint`              | ESLint, including `e2e/**`, `tests/**`, `scripts/**`                                                                                                                                                                                                                                                                                                                         |
| `npx tsc --noEmit -p .`     | type check. On a fresh clone run `npm run build` (or `npx next typegen`) first: the route types it needs are generated into `.next/types`                                                                                                                                                                                                                                    |
| `npm run build`             | production build and type check (also checks `e2e/**`)                                                                                                                                                                                                                                                                                                                       |
| `npm test`                  | unit and UI tests (`tests/unit`, `tests/ui`; the UI tests start Chromium against the built app and the stub API on the production routes, so run `npm run build` first)                                                                                                                                                                                                      |
| `npm run test:oidc`         | OIDC contract tests against a local stub issuer (login, refresh single-flight, PKCE, `PUBLIC_URL`, logout with and without an end-session endpoint)                                                                                                                                                                                                                          |
| `npm run test:upload`       | streams a generated 200 MiB body through the BFF against a stub API and checks memory (gate 128 MiB)                                                                                                                                                                                                                                                                         |
| `npm run e2e` or `make e2e` | Playwright against the running stack, real model calls (see below)                                                                                                                                                                                                                                                                                                           |
| `npm run test:production`   | production data adapter (`lib/data/http`) against the stub API through the real BFF (`tests/production/*.test.ts`, ports 3250-3257). Run `npm run build` first: without `.next/standalone/frontend/server.js` the tests stop with "run `npm run build` first". No backend, no model calls                                                                                    |
| `npm run e2e:production`    | Playwright on the production routes (`e2e-production/`) against the stub API: starts the built app on port 3260 with `scripts/serve-production.mjs`, signed in through the OIDC code flow against the in-process stub issuer (RS256 JWTs, port 3263; stub API 3261, test control 3262), so run `npm run build` first. Both viewports, no retries. No backend, no model calls |
| `npm run check:tokens`      | the live tree (`app/**`, `components/v2/**`, `components/shared/**`, `lib/**/*.tsx`) uses design tokens only (no literal colours, font sizes, families, radii, shadows, blur). It does NOT check spacing, sizes, `gap`, `padding` or `font-weight` literals                                                                                                                  |

Install the browser once: `npx --prefix frontend playwright install chromium`.

### End-to-end (Playwright)

`make e2e` is the live pack, run against the stack from `make up`. Every spec signs in as `alice@example.com` through the Keycloak
form, with `KEYCLOAK_QA_PASSWORD` from the shell or from `.env.local` (`make e2e` passes it as `E2E_ENV_FILE`; values are never
printed). It runs `frontend/e2e/signin.spec.ts` and `signout-expired-hint.spec.ts`:

- `signin.spec.ts`: sign in as alice, sign out (back on `/login?signedout=idp`, which signs in at once) and Keycloak shows its form again (the Keycloak session ended).
- `signout-expired-hint.spec.ts`: the same sign-out with an expired `id_token_hint` (about 5 minutes; the tab goes offline while idle so the page does not refresh its tokens): Keycloak shows no confirmation page.

They need the web app and the bundled Keycloak from `make up`, and spend no model calls (no project is created). Base
URL: `E2E_BASE_URL` (default `http://localhost:3001`). Reports and screenshots go to `frontend/logs/e2e/` (git-ignored).

## Environment variable names

Only names here; see `.env.example` for comments and `docs/configuration.md`
for roles and secret flags.

- Always: `APP_ENV`, `PUBLIC_URL` (no trailing slash), `API_INTERNAL_URL`, `SESSION_SECRET` (32 bytes or more), `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`.
- Optional: `OIDC_INTERNAL_URL`, `MCP_PUBLIC_URL`, `MCP_CLIENT_ID`, `MCP_SERVER_NAME` (the name the MCP dialog on the Integrations page registers the MCP server under; default `specflow`).
- Removed: `AUTH_MODE` is not a setting. `AUTH_MODE=dev` is refused: the failure line names `AUTH_MODE` and says to delete it. `AUTH_DEV_SIGNING_KEY` and `LOCAL_TEST_USER_EMAIL` are not read.
- Container only: `FRONTEND_PORT` (the host port); `PORT` and `HOSTNAME` are set by the image.
- No `NEXT_PUBLIC_*` variable is allowed (a test enforces it): everything is read at run time.

## Sign-in

One provider, in every environment: OpenID Connect (Authorization Code with PKCE, any provider, for example Keycloak). The
redirect URI is `<PUBLIC_URL>/api/auth/callback/oidc`. Refresh is single-flight per refresh token. Locally the provider is
the bundled Keycloak; the CI browser tests use a stub issuer that signs RS256 JWTs (`tests/oidc/stub-issuer.mjs`).

Sign-out is RP-initiated logout. The browser calls `POST /api/auth/end-session`, which answers `{"url": ...}`. The URL is the
IdP's logout URL (with `id_token_hint`, `client_id` and `post_logout_redirect_uri` = `<PUBLIC_URL>/login?signedout=idp`) only
when the session holds an ID token and the `end_session_endpoint` in the issuer's discovery document is on the origin of
`OIDC_ISSUER`. In every other case the URL is the local `<PUBLIC_URL>/login?signedout=1`: no session, a cross-site caller, an
IdP without an end-session endpoint (Google has none), an endpoint on a foreign origin, or a discovery error. The app
session ends either way. Keycloak 26 accepts an expired `id_token_hint`, so a sign-out after a long idle time still ends the
Keycloak session.

`/login` decides with `lib/login-mode.ts`: a session goes home; no session starts the sign-in at once (also on `?signedout=idp`, the
return from the IdP's logout, so the person sees the IdP's form straight away); `?error=` shows the fixed message and a button and
never redirects (no loop); `?signedout=1` (a local-only sign-out) shows "You are signed out." and a button and waits, since an
automatic sign-in would silently sign the person back in at Google. A flag with a session still present shows the still-signed-in note.

The browser holds only the session cookie (`specflow-session`, or `__Host-specflow-session` on https), never a token. Tokens are used only inside the BFF. The one exception is the ID token in the logout redirect URL, which RP-initiated logout needs.

## BFF rules

All browser calls go to `/api/backend/*` (`app/api/backend/[...path]/route.ts`), which
adds the Bearer token and forwards to the API.

- The route proxies with `node:http`/`node:https` and pipes the request stream. **Never use `fetch` to proxy**: it buffers or times out on large streamed uploads (a static test bans `fetch(` in the route).
- Upload is the raw `File` as the body with the canonical `Content-Type` of its extension (`lib/domain/upload-formats.ts`: PDF, DOCX, PPTX, XLSX, HTML, Markdown, CSV; never `file.type`) and `X-Requested-With: specflow`. No multipart; the app sets no size limit, the API refuses a file over its cap (PDF 100 MB, Office 50 MB, HTML, Markdown, CSV 10 MB) with 413 `file_too_large`.
- Mutations need a matching `Origin` and `X-Requested-With`; encoded path tricks (`%2f`, `%5c`, `%25`, `..`) get 400; cookies and forwarding headers are not passed on.
- `lib/gc-pacing.ts` is the memory mitigation for big uploads: while a body streams, it calls `gc()` every 8 MiB (best effort, no-op if unavailable). Without it, peak memory on a 200 MiB upload sometimes passed the 128 MiB gate on macOS (one run at +130 MiB). Keep it when touching the route.

## Test hooks

UI elements the tests use carry `data-testid`. Find them with `grep -rn data-testid components app`; do not rename one
without updating `tests/ui`, `e2e` and `e2e-production`. The V2 screens lean on roles, labels and names (the
`e2e-production` specs locate the header, the dialogs and the app cards that way); the test ids that exist:

| Where    | Hooks                                                                                                                                                                                                                                                         |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/login` | `login-page`, `login-oidc` (the "Sign in" button), `login-redirecting` (the quiet note while it starts), `login-signed-out`; `login-error` (a failed sign-in, fixed message), `login-still-signed-in` (note when the session is still valid after a sign-out) |
| Canvas   | `canvas-node` (a node; `data-state`, `data-kind`, `data-anchor`); per hub `hub-<id>` (`data-state`, `data-yellow`, `data-anchor`) and `hub-status-<id>`; per wire `wire-<key>` (`packages/ui`'s canvas)                                                       |

## Known limits

- Project and document lists read the first 100 entries only (no "load more").
- Real parse and summarize results have no screen: the summary is visible through the API only.

## Style checks

Light theme by default; dark through the cookie `specflow-theme=dark`. `/icon.svg` never needs login.

- `check:tokens` scans `app/**`, `components/v2/**`, `components/shared/**` and `lib/**/*.tsx` (the live tree). One
  exception per line: a trailing `// tokens-ok: <reason>`; the count is printed.
- `next build` may run while `next dev` serves (dev writes to `.next/dev`).

## Clone check config

`.jscpd.json` mirrors the kit `jscpd.json`.
