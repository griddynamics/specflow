# Spike: real clients against a Keycloak-protected MCP resource server

Reference for sessions 03, 04 and 06. Throwaway code that ran successfully in a cloud container on 2026-10-30
(Keycloak 26.4, MCP Python SDK 2.3.0, Claude Code CLI 2.1). It is not product code and is kept here as markdown so no
unowned source file lands in the repository. Lightly cleaned after the run: unused imports and debug prints dropped,
the password read from `QA_PASSWORD`, and the driver's end-of-login match set to the CLI's actual success line (the run
ended when the CLI exited). Delete this file with the rest of `plans/mcp-server/` after delivery.

Differences from the design, on purpose: the spike served MCP on port 8001, which is the worker health port; the
design uses 8002. The spike's environment names (`KC_ADMIN_PASSWORD`, `QA_PASSWORD`) are ad hoc; the harness uses the
`KEYCLOAK_*` names of session 03. The spike used the SDK's verifier; the product uses its own guard.

What it proved:

1. Claude Code completes the MCP OAuth flow (PRM discovery, Keycloak authorize with PKCE, token) with a pre-registered
   public client and a fixed callback port, driven headlessly through `claude mcp login --no-browser`.
2. Headless `claude -p` then calls the server's tool with that token.
3. The MCP Python SDK `OAuthClientProvider` does the same with the client information supplied up front (no
   registration request).
4. A plain-HTTP login against Keycloak works once its `Secure` cookies are forwarded by hand; no browser needed.

## Keycloak

```bash
docker run -d --name qa-keycloak -p 127.0.0.1:8180:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD="$KC_ADMIN_PASSWORD" \
  quay.io/keycloak/keycloak:26.4 start-dev
K="docker exec qa-keycloak /opt/keycloak/bin/kcadm.sh"
$K config credentials --server http://localhost:8080 --realm master --user admin --password "$KC_ADMIN_PASSWORD"
$K create realms -s realm=specflow -s enabled=true -s sslRequired=none
$K create client-scopes -r specflow -s name=specflow:read -s protocol=openid-connect -s 'attributes."include.in.token.scope"=true'
CID=$($K create clients -r specflow -s clientId=specflow-mcp -s publicClient=true -s standardFlowEnabled=true \
  -s directAccessGrantsEnabled=false -s 'redirectUris=["http://localhost:8765/callback","http://127.0.0.1:8765/callback"]' \
  -s 'attributes."pkce.code.challenge.method"=S256' -i)
$K create clients/$CID/protocol-mappers/models -r specflow -s name=mcp-audience -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper -s 'config."included.custom.audience"=http://localhost:8001/mcp' \
  -s 'config."access.token.claim"=true'
SID=$($K get client-scopes -r specflow --fields id,name | python3 -c 'import json,sys; print([s["id"] for s in json.load(sys.stdin) if s["name"]=="specflow:read"][0])')
$K update clients/$CID/optional-client-scopes/$SID -r specflow
$K create users -r specflow -s username=alice -s email=alice@example.com -s emailVerified=true -s enabled=true
$K set-password -r specflow --username alice --new-password "$QA_PASSWORD"
```

Resulting access token (decoded, values that matter): `iss=http://localhost:8180/realms/specflow`,
`aud=["http://localhost:8001/mcp","account"]`, `azp=specflow-mcp`, `typ=Bearer`,
`scope="offline_access profile email specflow:read"`, `email`, `email_verified=true`.

## Resource server (SDK 2.3.0, SDK auth, used only for the spike)

The product uses its own guard instead of the SDK verifier (`docs/mcp-server/auth.md`, AA3).

```python
import time

import jwt
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

ISSUER = "http://localhost:8180/realms/specflow"
RESOURCE = "http://localhost:8001/mcp"
jwks = jwt.PyJWKClient(f"{ISSUER}/protocol/openid-connect/certs")


class KeycloakVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER)
        except jwt.PyJWTError:
            return None
        return AccessToken(token=token, client_id=claims.get("azp", ""), scopes=claims.get("scope", "").split(),
                           expires_at=int(claims["exp"]), resource=RESOURCE)


server = MCPServer("specflow-spike", token_verifier=KeycloakVerifier(),
                   auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE,
                                     required_scopes=["specflow:read"], validate_token_resource=True))


@server.tool(name="whoami", description="Return the caller identity from the verified access token.")
def whoami() -> dict:
    token = get_access_token()
    claims = jwt.decode(token.token, options={"verify_signature": False}) if token else {}
    return {"sub": claims.get("sub"), "email": claims.get("email"), "client_id": token.client_id if token else None,
            "at": int(time.time())}


app = server.streamable_http_app(
    stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(allowed_hosts=["localhost:8001", "127.0.0.1:8001"], allowed_origins=[]),
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
```

Observed: `POST /mcp` without a token answers 401 with
`WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="http://localhost:8001/.well-known/oauth-protected-resource/mcp"`;
the PRM lists `resource`, `authorization_servers`, `scopes_supported`, `bearer_methods_supported`.

## Login helper (plain HTTP)

```python
import html
import re

import httpx

FORM = re.compile(r'<form[^>]*\baction="([^"]+)"', re.I)


def login(authorize_url: str, username: str, password: str) -> str:
    """Return the loopback redirect URL (code, state, iss) for an authorization URL."""

    cookies: dict[str, str] = {}

    def remember(response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):  # Secure cookies: httpx would drop them on http
            name, _, rest = header.partition("=")
            cookies[name.strip()] = rest.split(";", 1)[0]

    def jar() -> dict[str, str]:
        return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    with httpx.Client(follow_redirects=False, timeout=15) as http:
        response = http.get(authorize_url)
        for _ in range(5):
            remember(response)
            location = response.headers.get("location", "")
            if response.status_code in (302, 303) and re.match(r"http://(localhost|127\.0\.0\.1):\d+/", location):
                return location
            if response.status_code in (302, 303):
                response = http.get(location, headers=jar())
                continue
            match = FORM.search(response.text)
            if match is None:
                raise RuntimeError(f"no form on the page (status {response.status_code})")
            action = html.unescape(match.group(1))
            data = ({"username": username, "password": password, "credentialId": ""}
                    if 'name="password"' in response.text else {"accept": "Yes"})
            response = http.post(action, data=data, headers=jar())
    raise RuntimeError("no loopback redirect")
```

## MCP Python SDK client with a pre-registered public client

```python
import asyncio
import os
import sys
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata

URL, USER = sys.argv[1], sys.argv[2]
PASSWORD = os.environ["QA_PASSWORD"]
REDIRECT = "http://localhost:8765/callback"


class Memory:
    def __init__(self) -> None:
        self.tokens = None
        self.client = OAuthClientInformationFull(client_id="specflow-mcp", redirect_uris=[REDIRECT],
                                                 token_endpoint_auth_method="none")

    async def get_tokens(self): return self.tokens
    async def set_tokens(self, tokens): self.tokens = tokens
    async def get_client_info(self): return self.client
    async def set_client_info(self, info): self.client = info


captured: dict[str, str] = {}


async def redirect_handler(authorize_url: str) -> None:
    captured["url"] = await asyncio.to_thread(login, authorize_url, USER, PASSWORD)


async def callback_handler() -> AuthorizationCodeResult:
    query = parse_qs(urlsplit(captured["url"]).query)
    return AuthorizationCodeResult(code=query["code"][0], state=query.get("state", [None])[0],
                                   iss=query.get("iss", [None])[0])


async def main() -> None:
    auth = OAuthClientProvider(
        server_url=URL,
        client_metadata=OAuthClientMetadata(redirect_uris=[REDIRECT], token_endpoint_auth_method="none",
                                            grant_types=["authorization_code", "refresh_token"], response_types=["code"]),
        storage=Memory(), redirect_handler=redirect_handler, callback_handler=callback_handler,
    )
    async with (httpx2.AsyncClient(auth=auth, timeout=30) as http,
                streamable_http_client(URL, http_client=http) as (read, write),
                ClientSession(read, write) as session):
        await session.initialize()
        print("tools:", [t.name for t in (await session.list_tools()).tools])
        print("whoami:", (await session.call_tool("whoami", {})).content)


asyncio.run(main())
```

Note: the SDK's HTTP client is `httpx2` (a dependency of `mcp` 2.x), not `httpx`.

## Claude Code driver

```bash
claude mcp add --transport http -s local --client-id specflow-mcp --callback-port 8765 specflow http://localhost:8001/mcp
python3 claude_login.py specflow alice          # below; QA_PASSWORD in the environment
claude -p "Use the specflow whoami tool and reply with only the email it returns." --allowedTools "mcp__specflow__whoami"
claude mcp logout specflow && claude mcp remove specflow -s local
```

```python
import os
import pty
import re
import select
import sys
import time

NAME, USER = sys.argv[1], sys.argv[2]
PASSWORD = os.environ["QA_PASSWORD"]
# claude prints the authorization URL inside an OSC 8 hyperlink escape
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)")

pid, fd = pty.fork()
if pid == 0:
    os.execvp("claude", ["claude", "mcp", "login", NAME, "--no-browser"])

buffer, url, pasted, deadline = "", None, False, time.time() + 120
while time.time() < deadline:
    ready, _, _ = select.select([fd], [], [], 1)
    if ready:
        try:
            chunk = os.read(fd, 4096).decode(errors="replace")
        except OSError:
            break
        buffer += chunk
    if url is None and (match := re.search(r"(http://localhost:8180/realms/[^\s\x1b\x07\]]+)", buffer)):
        url = match.group(1)
        os.write(fd, (login(url, USER, PASSWORD) + "\r").encode())  # paste the redirect URL back
        pasted = True
    if pasted and re.search(r"(?i)authenticated with", ANSI.sub("", buffer[-400:])):
        break
os.waitpid(pid, 0)
```

Observed output: `Authenticated with "specflow". Its tools are now available in Claude Code.` The authorize URL
Claude Code built: `response_type=code`, `client_id=specflow-mcp`, PKCE S256, `redirect_uri=http://localhost:8765/callback`,
`scope=specflow:read offline_access`, `prompt=consent`, `resource=http://localhost:8001/mcp`.
