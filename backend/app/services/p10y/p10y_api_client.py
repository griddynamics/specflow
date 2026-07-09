"""
Internal API Client for SpecFlow
Provides a Python interface to the Internal API (p10y) for metrics and organization management.
Generated from OpenAPI specification.
"""

from datetime import date, datetime
from enum import Enum
import logging
import sys
import traceback
from typing import Any, Dict, List, Optional

import httpx

from app.services.p10y.p10y_api_models import MetricsResponse, UserSelfResponse

# Suppress verbose httpx/httpcore debug logs
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)

_REPOSITORY_LIST_PAGE_SIZE = 1000
_REPOSITORY_LIST_MAX_PAGES = 1000


def _response_total_pages(response: Dict[str, Any]) -> Optional[int]:
    """Extract a positive page count from a paginated P10Y response, if present."""
    total_pages = response.get("totalPages") or response.get("total_pages")
    if isinstance(total_pages, bool) or total_pages is None:
        return None
    try:
        parsed = int(total_pages)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class P10YInternalAPIClient:
    """
    Client for interacting with the P10Y Internal API.
    
    Provides methods for:
    - User authentication and authorization
    - Contributor metrics and leaderboards
    - Team metrics and analytics
    - Organization metrics
    - Repository management
    - Project and team management
    - Connection management
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: Optional[str] = None,
        timeout: float = 30.0
    ):
        """
        Initialize the API client.
        
        Args:
            base_url: Base URL of the API server
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout
        )

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers with authentication if available."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_query_params(self, params: Dict[str, Any]) -> List[tuple]:
        """
        Build query parameters, handling lists and None values.
        Returns a list of tuples to support array parameters with bracket notation.
        """
        result = []
        for key, value in params.items():
            if value is None or value == "":
                continue
            if isinstance(value, (list, tuple)):
                # For array parameters, add bracket notation and create multiple entries
                for item in value:
                    if isinstance(item, Enum):
                        result.append((f"{key}[]", item.value))
                    elif isinstance(item, (date, datetime)):
                        result.append((f"{key}[]", item.isoformat()))
                    else:
                        result.append((f"{key}[]", item))
            elif isinstance(value, Enum):
                result.append((key, value.value))
            elif isinstance(value, (date, datetime)):
                result.append((key, value.isoformat()))
            else:
                result.append((key, value))
        return result

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        requires_date: bool = False,
        **kwargs):
        """Make an HTTP request to the API.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON body data
            requires_date: If True, applies default date range (last 90 days) when
                          start_date and end_date are not provided. This is a workaround
                          for API endpoints that fail with empty timestamp errors.
            **kwargs: Additional arguments to pass to httpx
        """
        headers = self._build_headers()
        
        # Apply default dates if required and not provided
        if requires_date and params:
            from datetime import timedelta
            if params.get('start_date') is None and params.get('end_date') is None:
                end_date = datetime.now().date()
                start_date = end_date - timedelta(days=90)
                params = params.copy()  # Don't modify the original dict
                params['start_date'] = start_date
                params['end_date'] = end_date
        
        query_params = self._build_query_params(params or {})

        try:
            # httpx accepts both dict and list of tuples for params
            response = await self.client.request(method, endpoint, params=query_params, json=json_data, headers=headers, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            # Print full error details for HTTP errors
            print(f"\n{'='*80}", file=sys.stderr)
            print("HTTP ERROR DETAILS:", file=sys.stderr)
            print(f"{'='*80}", file=sys.stderr)
            print(f"Status Code: {e.response.status_code}", file=sys.stderr)
            print(f"Method: {method}", file=sys.stderr)
            print(f"Endpoint: {endpoint}", file=sys.stderr)
            print(f"URL: {e.response.url}", file=sys.stderr)
            print(f"Headers: {dict(e.response.headers)}", file=sys.stderr)
            print(f"Response Body:\n{e.response.text}", file=sys.stderr)
            print(f"Request Headers: {dict(e.request.headers)}", file=sys.stderr)
            if json_data:
                print(f"Request Body: {json_data}", file=sys.stderr)
            print(f"{'='*80}\n", file=sys.stderr)
            print(f"Full Exception:\n{traceback.format_exc()}", file=sys.stderr)
            raise
        except Exception as e:
            # Print full error details for other exceptions
            print(f"\n{'='*80}", file=sys.stderr)
            print("EXCEPTION ERROR DETAILS:", file=sys.stderr)
            print(f"{'='*80}", file=sys.stderr)
            print(f"Exception Type: {type(e).__name__}", file=sys.stderr)
            print(f"Exception Message: {str(e)}", file=sys.stderr)
            print(f"Method: {method}", file=sys.stderr)
            print(f"Endpoint: {endpoint}", file=sys.stderr)
            print(f"Full Traceback:\n{traceback.format_exc()}", file=sys.stderr)
            print(f"{'='*80}\n", file=sys.stderr)
            raise

    # ========== User Methods ==========

    async def get_current_user(self) -> UserSelfResponse:
        """Get current authenticated user information."""
        response = await self._make_request("GET", "/api/user/self")
        data = response.json()
        return UserSelfResponse(**data)

    # ========== Commit Methods ==========

    async def get_commit_stats(
        self,
        organisation_id: int,
        repository_ids: Optional[List[int]] = None,
        at_least: Optional[int] = None,
    ) -> MetricsResponse:
        """Get commit stats."""

        # TODO - for now lets just try to get everything in one page, if this gets too big we can paginate
        # We can perform pagination using the field "totalPages" after first request is done, we will know if there are more pages to fetch
        params = {
            "id_repository": repository_ids,
            "page": 1,
            "page_size": at_least,
        }
        response = await self._make_request(
            "GET",
            f"/api/organisation/{organisation_id}/commit-history",
            params=params,
        )
        data = response.json()
        return MetricsResponse(**data)

    # ========== Repository Methods ==========

    async def list_repositories(
        self,
        organisation_id: int,
        search: Optional[str] = None,
        status: Optional[str] = None,
        project_ids: Optional[List[int]] = None,
        page: int = 1,
        page_size: int = 50,
        light: bool = False,
        internal_statuses: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Get all repositories within an organization."""
        params = {
            "status": status,
            "id_project": project_ids,
            "page": page,
            "page_size": page_size,
            "light": light,
            "internal_status": internal_statuses,
            "search": search,
        }
        response = await self._make_request(
            "GET",
            f"/api/organisation/{organisation_id}/repository",
            params=params
        )
        return response.json()

    async def list_repositories_paginated(
        self,
        organisation_id: int,
        search: Optional[str] = None,
        page_size: int = _REPOSITORY_LIST_PAGE_SIZE,
        max_pages: int = _REPOSITORY_LIST_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Read all repository pages within an organization for the given search scope."""
        repositories: List[Dict[str, Any]] = []
        page = 1

        while True:
            repos_response = await self.list_repositories(
                organisation_id=organisation_id,
                search=search,
                page=page,
                page_size=page_size,
            )
            page_data = repos_response.get("data", [])
            repositories.extend(page_data)

            total_pages = _response_total_pages(repos_response)
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(page_data) < page_size:
                break

            page += 1
            if page > max_pages:
                raise RuntimeError(f"P10Y repository listing exceeded {max_pages} pages")

        return repositories

    async def update_repository(
        self,
        organisation_id: int,
        repository_id: int,
        repository_name: Optional[str] = None,
        git_url: Optional[str] = None,
        branch: Optional[str] = None,
        technology: Optional[str] = None,
        exclude_files: Optional[str] = None,
    ) -> None:
        """Update repository details."""
        json_data = {}
        if repository_name is not None:
            json_data["repository_name"] = repository_name
        if git_url is not None:
            json_data["git_url"] = git_url
        if branch is not None:
            json_data["branch"] = branch
        if technology is not None:
            json_data["technology"] = technology
        if exclude_files is not None:
            json_data["exclude_files"] = exclude_files
            
        await self._make_request(
            "PUT",
            f"/api/organisation/{organisation_id}/repository/{repository_id}",
            json_data=json_data
        )

    async def list_connections(self, organisation_id: int) -> Dict[str, Any]:
        """List the organisation's Git provider connections."""
        response = await self._make_request(
            "GET",
            f"/api/organisation/{organisation_id}/connection",
        )
        return response.json()

    async def sync_repositories(
        self,
        organisation_id: int,
        connection_id: Optional[int] = None,
    ) -> None:
        """Trigger an immediate repository re-fetch from the connected Git provider.

        Omitting ``connection_id`` may cause the API to return 400, but the sync still completes.
        """
        json_data: Dict[str, Any] = {}
        if connection_id is not None:
            json_data["connection_id"] = connection_id
        await self._make_request(
            "POST",
            f"/api/organisation/{organisation_id}/repository/sync",
            json_data=json_data or None,
        )

    async def enable_metrics(
        self,
        organisation_id: int,
        repository_ids: List[int],
    ) -> Dict[str, Any]:
        """Manually enable metrics calculation for repositories."""
        response = await self._make_request(
            "POST",
            f"/api/organisation/{organisation_id}/enable/metrics",
            json_data={"repo_ids": repository_ids}
        )
        return response.json()

    async def run_metrics(
        self,
        organisation_id: int,
        repository_ids: List[int],
    ) -> Dict[str, Any]:
        """Manually trigger metrics calculation for repositories."""
        response = await self._make_request(
            "POST",
            f"/api/organisation/{organisation_id}/metricRun",
            json_data={"repo_ids": repository_ids}
        )
        return response.json()


    async def close(self):
        """Close the HTTP client connection."""
        await self.client.aclose()

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        """Context manager exit."""
        await self.close()

