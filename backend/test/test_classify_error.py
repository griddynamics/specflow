"""
Tests for model_routing.classify_error and extract_http_status_from_message.

Covers: tool_call_failure, model_routing_failure, api_error_status exclusion,
exception-path HTTP-status inference, pattern priority, and new routing patterns.
"""
from app.schemas.agent import AgentErrorType
from app.services.model_routing import classify_error, extract_http_status_from_message


class TestToolCallFailure:
    def test_tool_use_keyword(self):
        assert classify_error("error with tool_use block") == AgentErrorType.TOOL_CALL_FAILURE

    def test_does_not_support_tools(self):
        assert classify_error("This model does not support tools") == AgentErrorType.TOOL_CALL_FAILURE

    def test_function_call(self):
        assert classify_error("invalid function_call format") == AgentErrorType.TOOL_CALL_FAILURE

    def test_case_insensitive(self):
        assert classify_error("TOOL_USE not supported") == AgentErrorType.TOOL_CALL_FAILURE

    def test_tool_call_not_affected_by_api_error_status(self):
        # api_error_status guard only skips the routing check, not tool-call check.
        assert classify_error("tool_use block invalid", api_error_status=500) == AgentErrorType.TOOL_CALL_FAILURE


class TestModelRoutingFailure:
    def test_api_error_prefix(self):
        msg = "API Error: API returned an empty or malformed response (HTTP 200) — check for a proxy or gateway intercepting the request"
        assert classify_error(msg) == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_api_error_alone_no_longer_classifies(self):
        # "api error" was removed from the pattern list — too broad, matched genuine HTTP errors.
        assert classify_error("api error: something went wrong") is None

    def test_malformed_response_pattern(self):
        assert classify_error("malformed response received from upstream") == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_empty_response_pattern(self):
        assert classify_error("empty response from server") == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_malformed_response_case_insensitive(self):
        assert classify_error("MALFORMED RESPONSE body") == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_empty_response_case_insensitive(self):
        assert classify_error("EMPTY RESPONSE received") == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_does_not_match_unrelated_errors(self):
        # "rate limit" and generic "timeout" are intentionally NOT connection errors (out of
        # scope for the transient-connection retry). "connection refused" now classifies as
        # CONNECTION_ERROR — see TestConnectionError.
        assert classify_error("rate limit exceeded") is None
        assert classify_error("timeout after 30s") is None

    def test_tool_call_takes_priority_over_routing(self):
        # Both patterns present; tool_call_failure check runs first.
        msg = "tool_use failed: API Error: routing issue"
        assert classify_error(msg) == AgentErrorType.TOOL_CALL_FAILURE


class TestConnectionError:
    """Transient network/connection failures — retryable, HTTP-status-independent."""

    def test_unable_to_connect_to_api(self):
        # The exact string the SDK emits when the laptop has no internet.
        assert classify_error("API Error: Unable to connect to API (ConnectionRefused)") == AgentErrorType.CONNECTION_ERROR

    def test_socket_connection_closed(self):
        assert classify_error("API Error: The socket connection was closed unexpectedly.") == AgentErrorType.CONNECTION_ERROR

    def test_connection_refused(self):
        assert classify_error("connection refused") == AgentErrorType.CONNECTION_ERROR

    def test_connection_reset(self):
        assert classify_error("Connection reset by peer") == AgentErrorType.CONNECTION_ERROR

    def test_server_disconnected(self):
        assert classify_error("Server disconnected without sending a response.") == AgentErrorType.CONNECTION_ERROR

    def test_case_insensitive(self):
        assert classify_error("UNABLE TO CONNECT TO API") == AgentErrorType.CONNECTION_ERROR

    def test_connection_wins_over_spurious_status_in_message(self):
        # A stray number must not demote a real connection error to unclassified.
        msg = "Unable to connect to API (ConnectionRefused) after 500ms"
        inferred = extract_http_status_from_message(msg)
        assert inferred == 500  # extractor would pick this up...
        assert classify_error(msg, api_error_status=inferred) == AgentErrorType.CONNECTION_ERROR  # ...but connection still wins

    def test_tool_call_takes_priority_over_connection(self):
        # tool-call incompatibility must abort immediately, never be retried as transient.
        assert classify_error("tool_use failed: connection error") == AgentErrorType.TOOL_CALL_FAILURE


class TestApiErrorStatusExclusion:
    def test_known_http_error_not_classified_as_routing_failure(self):
        msg = "API Error: upstream overloaded"
        assert classify_error(msg, api_error_status=529) is None

    def test_rate_limit_status_not_routing_failure(self):
        msg = "API Error: rate limit hit"
        assert classify_error(msg, api_error_status=429) is None

    def test_server_error_status_not_routing_failure(self):
        msg = "API Error: internal server error"
        assert classify_error(msg, api_error_status=500) is None

    def test_none_status_still_classifies(self):
        msg = "API Error: empty response"
        assert classify_error(msg, api_error_status=None) == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_status_200_does_not_exclude_routing_failure(self):
        # Routing failures ARE HTTP 200 with a malformed body — must not be excluded.
        msg = "API Error: malformed response"
        assert classify_error(msg, api_error_status=200) == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_status_below_400_does_not_exclude(self):
        msg = "API Error: malformed response, redirect issue"
        assert classify_error(msg, api_error_status=301) == AgentErrorType.MODEL_ROUTING_FAILURE


class TestExtractHttpStatusFromMessage:
    def test_extracts_429(self):
        assert extract_http_status_from_message("API Error: rate limit (HTTP 429)") == 429

    def test_extracts_500(self):
        assert extract_http_status_from_message("API Error: internal server error (HTTP 500)") == 500

    def test_extracts_529(self):
        assert extract_http_status_from_message("upstream overloaded (HTTP 529)") == 529

    def test_returns_none_for_no_status(self):
        assert extract_http_status_from_message("API Error: something went wrong") is None

    def test_returns_none_for_200(self):
        # 200 is not 4xx/5xx — returns None so routing failure check proceeds normally.
        assert extract_http_status_from_message("API Error: empty response (HTTP 200)") is None

    def test_returns_none_for_empty_string(self):
        assert extract_http_status_from_message("") is None

    def test_extracts_first_4xx_5xx(self):
        # If multiple codes appear, first 4xx/5xx wins.
        assert extract_http_status_from_message("Error 429: also saw 500") == 429


class TestExceptionPathStatusInference:
    """
    Verify that _classify_error, when called with a status inferred from the message
    (the exception path where api_error_status is unavailable), correctly excludes
    genuine HTTP errors and still classifies routing failures.
    """

    def test_429_in_message_excluded(self):
        msg = "API Error: rate limit hit (HTTP 429)"
        inferred = extract_http_status_from_message(msg)
        assert classify_error(msg, api_error_status=inferred) is None

    def test_500_in_message_excluded(self):
        msg = "API Error: internal server error (HTTP 500)"
        inferred = extract_http_status_from_message(msg)
        assert classify_error(msg, api_error_status=inferred) is None

    def test_529_in_message_excluded(self):
        msg = "API Error: upstream overloaded (HTTP 529)"
        inferred = extract_http_status_from_message(msg)
        assert classify_error(msg, api_error_status=inferred) is None

    def test_200_in_message_still_routing_failure(self):
        # Malformed 200: no 4xx/5xx extracted → status stays None → routing failure detected.
        msg = "API Error: API returned an empty or malformed response (HTTP 200)"
        inferred = extract_http_status_from_message(msg)
        assert inferred is None
        assert classify_error(msg, api_error_status=inferred) == AgentErrorType.MODEL_ROUTING_FAILURE

    def test_no_status_in_message_classifies_as_routing_failure(self):
        msg = "API Error: empty response"
        inferred = extract_http_status_from_message(msg)
        assert classify_error(msg, api_error_status=inferred) == AgentErrorType.MODEL_ROUTING_FAILURE


class TestNoClassification:
    def test_empty_string(self):
        assert classify_error("") is None

    def test_generic_error(self):
        assert classify_error("Something went wrong") is None
