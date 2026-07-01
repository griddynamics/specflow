"""Tests for backend structured-rejection passthrough (services/specflow_backend.py).

A backend HTTP 400 whose ``detail`` is a structured rejection (carries a ``code``)
must surface as a typed BackendContractRejection — not a generic exception — so the
MCP can render the same actionable message + code as an MCP-side precheck.
"""

from contextlib import asynccontextmanager

import httpx
import pytest
from unittest.mock import patch

from services.specflow_backend import (
    BackendContractRejection,
    SpecFlowBackendService,
    _parse_contract_rejection,
)


def _response(status: int, json_body) -> httpx.Response:
    return httpx.Response(
        status, json=json_body, request=httpx.Request("POST", "http://backend/api")
    )


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def post(self, *args, **kwargs):
        return self._resp

    async def get(self, *args, **kwargs):
        return self._resp

    async def delete(self, *args, **kwargs):
        return self._resp


def _patch_client(resp):
    @asynccontextmanager
    async def _cm(*args, **kwargs):
        yield _FakeClient(resp)

    return patch.object(SpecFlowBackendService, "_client", lambda self, *a, **k: _cm())


class TestParseContractRejection:
    def test_structured_detail_returned(self):
        resp = _response(400, {"detail": {"code": "PLAN_NO_PHASES", "error": "no phases"}})
        parsed = _parse_contract_rejection(resp)
        assert parsed == {"code": "PLAN_NO_PHASES", "error": "no phases"}

    def test_string_detail_is_not_a_rejection(self):
        resp = _response(400, {"detail": "Invalid params JSON"})
        assert _parse_contract_rejection(resp) is None

    def test_dict_detail_without_code_is_not_a_rejection(self):
        resp = _response(400, {"detail": {"error": "something"}})
        assert _parse_contract_rejection(resp) is None

    def test_non_json_body_is_not_a_rejection(self):
        resp = httpx.Response(
            500, text="<html>boom</html>", request=httpx.Request("POST", "http://x")
        )
        assert _parse_contract_rejection(resp) is None


class TestUploadFileRejectionPassthrough:
    @pytest.mark.asyncio
    async def test_structured_400_raises_typed_rejection(self):
        resp = _response(
            400,
            {"detail": {"code": "PLAN_UNPARSEABLE", "error": "Couldn't parse plan."}},
        )
        with _patch_client(resp):
            svc = SpecFlowBackendService()
            with pytest.raises(BackendContractRejection) as exc_info:
                await svc.upload_file(
                    endpoint="/api/v1/workspace/sync",
                    file_data=b"x",
                    filename="f.tar.gz",
                    form_data={},
                )
        assert exc_info.value.detail["code"] == "PLAN_UNPARSEABLE"
        assert "parse" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_unstructured_400_raises_generic_exception(self):
        resp = _response(400, {"detail": "Archive must be .tar.gz or .tgz format"})
        with _patch_client(resp):
            svc = SpecFlowBackendService()
            with pytest.raises(Exception) as exc_info:
                await svc.upload_file(
                    endpoint="/api/v1/workspace/sync",
                    file_data=b"x",
                    filename="f.tar.gz",
                    form_data={},
                )
        assert not isinstance(exc_info.value, BackendContractRejection)

    @pytest.mark.asyncio
    async def test_success_returns_body_text(self):
        resp = _response(201, {"generation_id": "est-1", "workspace_ids": ["ws-1"]})
        with _patch_client(resp):
            svc = SpecFlowBackendService()
            text = await svc.upload_file(
                endpoint="/api/v1/workspace/sync",
                file_data=b"x",
                filename="f.tar.gz",
                form_data={},
            )
        assert "est-1" in text


class TestCallBackendErrorHandling:
    """call_backend() must raise on HTTP errors, not return a non-JSON string.

    Regression coverage for the bug where a 4xx/5xx response was swallowed into
    a plain "Error calling backend: ..." string; callers that json.loads() the
    return value then failed with a misleading "Expecting value: line 1 column 1
    (char 0)" instead of ever seeing the actual backend error.
    """

    @pytest.mark.asyncio
    async def test_success_returns_body_text(self):
        resp = _response(200, {"status": "running"})
        with _patch_client(resp):
            svc = SpecFlowBackendService()
            text = await svc.call_backend(endpoint="/api/v1/generation-sessions/est-1/status", method="GET")
        assert "running" in text

    @pytest.mark.asyncio
    async def test_plain_error_detail_surfaces_in_exception_message(self):
        resp = _response(
            500,
            {"detail": "Cannot retry generation est-1: retry_count (3) has reached max_retries (3)."},
        )
        with _patch_client(resp):
            svc = SpecFlowBackendService()
            with pytest.raises(Exception) as exc_info:
                await svc.call_backend(endpoint="/api/v1/generation-sessions/est-1/retry", method="POST")
        assert not isinstance(exc_info.value, BackendContractRejection)
        assert "max_retries" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_structured_400_raises_typed_rejection(self):
        resp = _response(400, {"detail": {"code": "PLAN_NO_PHASES", "error": "no phases"}})
        with _patch_client(resp):
            svc = SpecFlowBackendService()
            with pytest.raises(BackendContractRejection) as exc_info:
                await svc.call_backend(endpoint="/api/v1/generation-sessions/sync", method="POST")
        assert exc_info.value.detail["code"] == "PLAN_NO_PHASES"

    @pytest.mark.asyncio
    async def test_connection_error_raises_not_returns_string(self):
        @asynccontextmanager
        async def _cm(*args, **kwargs):
            class _RaisingClient:
                async def get(self, *a, **k):
                    raise httpx.ConnectError("connection refused")

            yield _RaisingClient()

        with patch.object(SpecFlowBackendService, "_client", lambda self, *a, **k: _cm()):
            svc = SpecFlowBackendService()
            with pytest.raises(Exception) as exc_info:
                await svc.call_backend(endpoint="/api/v1/generation-sessions/est-1/status", method="GET")
        assert "connection refused" in str(exc_info.value)
