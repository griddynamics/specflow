# 02 — Auth claim profile and scopes

Depends on: none. Parallel with 01, 03.

## Do

1. Add the claim profile settings to `AuthSettings` exactly as in `docs/mcp-server/auth.md` (section "Claim profile for
   any OIDC provider"): `AUTH_SUBJECT_CLAIM`, `AUTH_CLIENT_ID_CLAIM`, `AUTH_EMAIL_CLAIM`, `AUTH_EMAIL_VERIFIED_CLAIM`,
   `AUTH_TOKEN_TYPE`, `AUTH_IGNORED_AUDIENCES`, with today's behavior as defaults.
2. `ClaimPolicy` carries the profile and `required_scopes`; `claims.validate` applies them in the order of auth.md
   "Token validation"; `Principal` gains `scopes` (union of `scope` and `scp`).
3. Missing scope raises a 403 `insufficient_scope` `AuthError` whose headers carry the scope challenge; email-policy
   403s carry no challenge (auth.md "Failure responses").
4. `AUTH_TOKEN_TYPE=rfc9068` needs the JOSE header `typ`: the verifiers hand it to validation; signature-only decoding stays.
5. Settings guard: an audience equal to an allowed client id is refused (REST policy now; the MCP policy in 04 reuses it).
6. Rows for the six variables in `docs/configuration.md`; the `insufficient_scope` code in `backend/archkit.toml`.

## Subagents

- auth-reviewer (short-term): reviews the claim rules against auth.md and the identity-access checklist (audience,
  issuer, expiry, key rotation, ID-token confusion).

## Rules

- With default settings, every existing auth, settings and API test passes unchanged; dev tokens minted by
  `frontend/lib/dev-token.ts` stay valid.
- The REST policy requires no scopes; REST answers never change.
- `IGNORED_AUDIENCES` stops being a constant only through the setting; default stays `account`.
- Do not change user keying (`users.auth_subject`), the bearer parser, `MAX_TOKEN_BYTES` or the JWKS cache.
- `ConfigurationError` names every bad variable at once (existing `load_settings` pattern).
- No frontend change.

## Done when

- A token shaped like each provider recipe in auth.md (Keycloak, Auth0, Okta, Entra ID) validates with the matching
  profile and fails with the default profile where the shapes differ.
- Scope union of `scope` string and `scp` string or list is on the `Principal`; missing required scope is 403 with the challenge.
- Defaults keep REST behavior byte-identical in the existing test suite.

## Checklist

- [ ] Six settings with validation (non-blank claim names; `AUTH_TOKEN_TYPE` enum; CSV audiences)
- [ ] `ClaimPolicy.build`/`from_settings` take the profile and `required_scopes`
- [ ] Subject read from `AUTH_SUBJECT_CLAIM`; blank or non-string refused (401)
- [ ] Client read from `AUTH_CLIENT_ID_CLAIM`
- [ ] Email read from `AUTH_EMAIL_CLAIM`; verified flag skipped when the setting is empty
- [ ] Token type: `keycloak` claim, `rfc9068` header (`at+jwt`, `application/at+jwt`), `none`
- [ ] Ignored audiences from settings; exact single audience rule kept
- [ ] `Principal.scopes` tuple; `scope` space-split; `scp` string or list; duplicates removed
- [ ] `insufficient_scope` 403 code, fixed message, challenge header with `scope=`
- [ ] Email-policy 403s without challenge header
- [ ] Audience-equals-client-id guard with a test
- [ ] Recipe-shaped token tests for the four providers (stub JWKS from `specflow_auth_support`)
- [ ] ID token (aud = client id) rejected under `AUTH_TOKEN_TYPE=none`
- [ ] Existing auth, settings, API tests unchanged and green
- [ ] Dev-token path unchanged (frontend claims shape)
- [ ] `backend/archkit.toml` error code registry updated; `make validate-python-arch` green
- [ ] `docs/configuration.md` rows; docs bundle check passes
- [ ] No token or claim value in any log event or error message
- [ ] `make check`, `make validate-python-lint`, `make validate-python-scripts`
- [ ] Unit coverage of `auth/claims.py` branches complete
- [ ] `02-handoff.md` written; `HANDOFF.md` row updated
