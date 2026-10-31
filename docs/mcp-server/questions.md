---
type: Questions
title: "Open questions of the MCP server"
description: "Open questions of the MCP server specification, each with the default that applies until it is answered"
status: draft
tags: [mcp]
---
# questions.md: open questions of the MCP server specification

The specification follows each default below until its question is answered. An answered question leaves this list,
and its answer is written into the document the row names. None of them blocks implementation.

| # | Question | Default until answered |
|---|---|---|
| Q4 | Does the production identity provider work with the claim profile, and what is still unverified about its client setup? Answered by the provider's operators: PKCE is allowed; access tokens are JWTs with `iss`, `aud` and `azp` equal to the client id, `sub` equal to the user's email, a claim that marks access tokens, `scp`, `exp`, `iat`, `nbf` (about one hour lifetime), `jti` and provider-specific claims, and no `email`, email-verified or `typ` claim; the code flow gives the token reaching `/mcp` the client id as audience, and a resource-server audience comes only from a token exchange that Claude Code does not do (handled by AA8 in [auth.md](auth.md)). Still open, to verify in the production rollout with the QA harness: a public client with no secret; the loopback redirects `http://localhost:8765/callback` and `http://127.0.0.1:8765/callback` (the operators are not sure); the JWT header (`alg` RS256 or ES256, `kid`) and a JWKS on the issuer's host; whether the code-flow token carries the same claims, including the type marker, while ID tokens do not; whether the provider accepts the `resource` parameter and the `offline_access` scope that Claude Code always sends; the refresh-token lifetime. | Proceed: one pre-registered public client ([auth.md](auth.md), client registration) with `AUTH_TOKEN_TYPE=claim`, `MCP_AUDIENCE` equal to the client id and the email as subject. If loopback redirects are refused, the provider is brokered through Keycloak, which accepts them. If the code-flow token lacks the type marker, the token-type check cannot tell ID tokens apart and the design is revisited. Either way the design is revisited when the answers arrive |
