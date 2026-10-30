# 02 — Auth claim profile

Depends on: none. Parallel with 01, 03.

## Do

1. Add the claim profile settings to `AuthSettings` exactly as in `docs/mcp-server/auth.md` (section "Claim profile for
   any OIDC provider"): `AUTH_SUBJECT_CLAIM`, `AUTH_CLIENT_ID_CLAIM`, `AUTH_EMAIL_CLAIM`, `AUTH_EMAIL_VERIFIED_CLAIM`,
   `AUTH_TOKEN_TYPE`, `AUTH_IGNORED_AUDIENCES`, with the provider-neutral defaults of auth.md (`AUTH_TOKEN_TYPE=none`,
   `AUTH_CLIENT_ID_CLAIM=azp,client_id`).
2. `ClaimPolicy` carries the profile; `claims.validate` applies it in the order of auth.md "Token validation".
   Email-policy 403s carry no challenge (auth.md "Failure responses"). No scope is read or checked.
3. `AUTH_TOKEN_TYPE=rfc9068` needs the JOSE header `typ`: the verifiers hand it to validation; signature-only decoding stays.
4. Settings guards: an audience equal to an allowed client id, and `AUTH_IGNORED_AUDIENCES` holding
   `AUTH_API_AUDIENCE`, are refused (REST policy now; 04 adds the MCP-side checks from auth.md).
5. Move `bearer_token` (and its charset and size constants) from `auth.dependencies` into a framework-free `auth`
   module, behavior unchanged; `auth.dependencies` uses it from there. The `mcp` role must import it without FastAPI.
6. Rows for the six variables in `docs/configuration.md`.

## Subagents

- auth-reviewer (short-term): reviews the claim rules against auth.md and the identity-access checklist (audience,
  issuer, expiry, key rotation, ID-token confusion).

## Rules

- With `AUTH_TOKEN_TYPE=keycloak`, every existing auth, settings and API test passes unchanged; the existing tests of
  the `typ` claim run with that setting. The neutral defaults change one REST behavior on purpose: a token without
  `typ: Bearer` is no longer refused for that alone. Dev tokens minted by `frontend/lib/dev-token.ts` stay valid.
- REST answers never change.
- `IGNORED_AUDIENCES` stops being a constant only through the setting; default stays `account`.
- Keep today's check order (auth.md "Token validation" documents it): subject, client, token type; then email
  verified before email validity before domain. Reordering changes which 403 code REST returns.
- Do not change user keying (`users.auth_subject`), the bearer parser's behavior, `MAX_TOKEN_BYTES` or the JWKS cache.
- Entra ID v2 tokens carry the API registration's application id (a GUID) in `aud` and the client registration's id
  in `azp`; test that shape, not an App ID URI audience.
- `ConfigurationError` names every bad variable at once (existing `load_settings` pattern).
- No frontend change.

## Done when

- A token shaped like each provider recipe in auth.md (Keycloak, Auth0, Okta, Entra ID) validates with the matching
  profile and fails with the default profile where the shapes differ.
- With `AUTH_TOKEN_TYPE=keycloak`, REST behavior is byte-identical in the existing test suite; with the defaults only the
  `typ` check is gone.
- `auth.dependencies` is the only `auth` module that imports FastAPI; the bearer parser is importable without it.
- The handoff maps FR-MCP-AUTH-08 (and the REST half of FR-MCP-AUTH-07) to their tests.

## Checklist

- [ ] Six settings with validation (non-blank claim names; `AUTH_TOKEN_TYPE` enum; CSV audiences)
- [ ] `ClaimPolicy.build`/`from_settings` take the profile
- [ ] Subject read from `AUTH_SUBJECT_CLAIM`; blank or non-string refused (401)
- [ ] Client read from the first present claim of `AUTH_CLIENT_ID_CLAIM` (default `azp,client_id`)
- [ ] Email read from `AUTH_EMAIL_CLAIM`; verified flag skipped when the setting is empty
- [ ] Token type: `none` (default), `keycloak` claim, `rfc9068` header (`at+jwt`, `application/at+jwt`)
- [ ] Ignored audiences from settings; exact single audience rule kept
- [ ] Email-policy 403s without challenge header
- [ ] Audience-equals-client-id guard with a test
- [ ] Ignored audiences may not hold `AUTH_API_AUDIENCE` (guard and test)
- [ ] `bearer_token` moved to a framework-free module; existing parser tests unchanged and green
- [ ] Check order unchanged: an unverified token without email still answers `email_not_verified`
- [ ] Recipe-shaped token tests for the four providers (stub JWKS from `specflow_auth_support`)
- [ ] ID token (aud = client id) rejected under `AUTH_TOKEN_TYPE=none`
- [ ] Existing auth, settings, API tests unchanged and green
- [ ] Dev-token path unchanged (frontend claims shape)
- [ ] `make validate-python-arch` green
- [ ] `docs/configuration.md` rows; docs bundle check passes
- [ ] No token or claim value in any log event or error message
- [ ] `make check`, `make validate-python-lint`, `make validate-python-scripts`
- [ ] Unit coverage of `auth/claims.py` branches complete
- [ ] `02-handoff.md` written; `HANDOFF.md` row updated
