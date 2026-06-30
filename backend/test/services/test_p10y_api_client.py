"""
Tests for P10YInternalAPIClient user/organisation resolution.

Scenario: the org id is bound to the API key and is read from GET /api/user/self
(``organisationId`` on the response). This is what specflow-init.sh uses to resolve
the org id once and persist it to .env.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from app.services.p10y.p10y_api_client import P10YInternalAPIClient


def _user_response() -> Mock:
    response = Mock()
    response.json = Mock(
        return_value={
            "id": 20,
            "email": "user@test.com",
            "isMfaActive": False,
            "organisationId": 123,
            "role": "ADMINISTRATOR",
        }
    )
    return response


@pytest.mark.asyncio
async def test_get_current_user_exposes_organisation_id() -> None:
    """get_current_user() calls /api/user/self and exposes organisationId."""
    client = P10YInternalAPIClient(base_url="https://p10y.test", api_key="key")
    client._make_request = AsyncMock(return_value=_user_response())  # type: ignore[method-assign]

    user = await client.get_current_user()

    assert user.organisationId == 123
    client._make_request.assert_awaited_once_with("GET", "/api/user/self")


@pytest.mark.asyncio
async def test_sync_repositories_posts_to_sync_endpoint_without_connection() -> None:
    """sync_repositories() POSTs to /repository/sync with no body when no connection given."""
    client = P10YInternalAPIClient(base_url="https://p10y.test", api_key="key")
    client._make_request = AsyncMock(return_value=Mock())  # type: ignore[method-assign]

    await client.sync_repositories(42)

    client._make_request.assert_awaited_once_with(
        "POST", "/api/organisation/42/repository/sync", json_data=None
    )


@pytest.mark.asyncio
async def test_sync_repositories_includes_connection_id_when_provided() -> None:
    """sync_repositories() sends {'connection_id': N} when a connection id is supplied."""
    client = P10YInternalAPIClient(base_url="https://p10y.test", api_key="key")
    client._make_request = AsyncMock(return_value=Mock())  # type: ignore[method-assign]

    await client.sync_repositories(42, connection_id=7)

    client._make_request.assert_awaited_once_with(
        "POST", "/api/organisation/42/repository/sync", json_data={"connection_id": 7}
    )
