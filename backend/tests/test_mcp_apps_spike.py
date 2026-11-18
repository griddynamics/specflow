"""The MCP Apps spike (docs/mcp-server/apps.md "Delivery", P0): the view and its two tools over the real HTTP app."""

from __future__ import annotations

from mcp.server.apps import APP_MIME_TYPE

from mcp_server.registry import SPIKE_TICKET_KEY, SPIKE_VIEW_URI
from mcp_support import ALICE, ALICE_EMAIL, build_app, call, mcp_env, sdk_session


def spike_app(valid_env):
    return build_app(mcp_env(valid_env), tools=None)  # no hook: the registry and the spike, as the role serves them


async def test_both_spike_tools_name_the_view_and_whoami_is_for_the_view_only(valid_env):
    async with sdk_session(spike_app(valid_env), ALICE) as session:
        listed = {t.name: t for t in (await session.list_tools()).tools}
    assert listed["spike_open_view"].meta == {"ui": {"resourceUri": SPIKE_VIEW_URI}}
    assert listed["spike_whoami"].meta == {"ui": {"resourceUri": SPIKE_VIEW_URI, "visibility": ["app"]}}


async def test_the_view_is_one_html_document_of_the_apps_mime_type(valid_env):
    async with sdk_session(spike_app(valid_env), ALICE) as session:
        read = await session.read_resource(SPIKE_VIEW_URI)
    (content,) = read.contents
    assert content.mime_type == APP_MIME_TYPE
    html = getattr(content, "text", "")
    assert html.startswith("<!doctype html>") and '"ui/initialize"' in html and "spike_whoami" in html
    assert "<script src" not in html and "innerHTML" not in html  # one self-contained file, text only


async def test_opening_the_view_puts_a_fresh_ticket_only_in_the_results_meta(valid_env):
    async with sdk_session(spike_app(valid_env), ALICE) as session:
        first = await call(session, "spike_open_view")
        second = await call(session, "spike_open_view")
    ticket = (first.meta or {}).get(SPIKE_TICKET_KEY)
    assert isinstance(ticket, str) and len(ticket) == 8
    assert ticket != (second.meta or {}).get(SPIKE_TICKET_KEY)
    assert ticket not in first.model_dump_json(exclude={"meta"})
    assert first.structured_content is not None and first.structured_content["apps_supported"] is False


async def test_whoami_names_the_tokens_user(valid_env):
    async with sdk_session(spike_app(valid_env), ALICE) as session:
        result = await call(session, "spike_whoami")
    assert not result.is_error
    assert result.structured_content is not None and result.structured_content["email"] == ALICE_EMAIL
