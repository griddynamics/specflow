"""Bearer-token authentication for the specflow API (OIDC tokens).

The signature check (``OidcJwtVerifier``) ends in the
claim-validation path (``claims.validate``). Failures are ``AuthError``s with a
fixed status, code and message that never carry token content.
"""
