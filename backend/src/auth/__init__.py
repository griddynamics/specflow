"""Bearer-token authentication for the specflow API (OIDC or local dev tokens).

Two signature checks (``OidcJwtVerifier``, ``DevTokenVerifier``) end in one
claim-validation path (``claims.validate``). Failures are ``AuthError``s with a
fixed status, code and message that never carry token content.
"""
