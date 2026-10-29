"""Fixtures for gateway.yml, paired with it by stem for `semgrep --test`.

`--test` ignores `paths:`, so the patterns themselves are exercised here; the path scoping of
`kit.configurable.sdk-import-outside-gateway` is proved on `fixtures/src/` by a real scan."""

from __future__ import annotations

# --- kit.configurable.sdk-import-outside-gateway ---

# ruleid: kit.configurable.sdk-import-outside-gateway
import vendor_sdk

# ruleid: kit.configurable.sdk-import-outside-gateway
from vendor_sdk import client

# ok: kit.configurable.sdk-import-outside-gateway
import httpx

_ = (vendor_sdk, client, httpx)

# --- kit.configurable.no-silent-fallback-in-gateway ---

# The pattern spans the whole try/except, so the reported line is the `try:` line itself.


async def send_with_fallback(client: object, payload: object) -> object:
    # ruleid: kit.configurable.no-silent-fallback-in-gateway
    try:
        return await client.send(payload)
    except GatewayUnavailable:
        return {"items": []}


async def send_and_raise(client: object, payload: object) -> object:
    # ok: kit.configurable.no-silent-fallback-in-gateway
    try:
        return await client.send(payload)
    except GatewayUnavailable as exc:
        raise UpstreamUnavailable("gateway unavailable") from exc


class GatewayUnavailable(Exception): ...


class UpstreamUnavailable(Exception): ...
