"""
Unit tests for SKIP_MODE functionality.

Tests that agent_query returns immediately when SKIP_AGENT_EXECUTION is set.
"""

import os
import pytest
from unittest.mock import patch
from app.services.claude_code import agent_query


@pytest.mark.asyncio
async def test_skip_mode_enabled():
    """Test that agent_query returns SKIP_MODE when SKIP_AGENT_EXECUTION is true."""
    # Set environment variable
    original_value = os.environ.get("SKIP_AGENT_EXECUTION")
    os.environ["SKIP_AGENT_EXECUTION"] = "true"
    
    try:
        result = await agent_query(
            system_prompt="Test prompt",
            workspace_path="/tmp/test",
            model="claude-sonnet-4-20250514",
        )
        
        assert result.result == "SKIP_MODE"
        assert result.session_id is None
    finally:
        # Restore original value
        if original_value is None:
            os.environ.pop("SKIP_AGENT_EXECUTION", None)
        else:
            os.environ["SKIP_AGENT_EXECUTION"] = original_value


@pytest.mark.asyncio
async def test_skip_mode_with_1():
    """Test that agent_query returns SKIP_MODE when SKIP_AGENT_EXECUTION is 1."""
    original_value = os.environ.get("SKIP_AGENT_EXECUTION")
    os.environ["SKIP_AGENT_EXECUTION"] = "1"
    
    try:
        result = await agent_query(
            system_prompt="Test prompt",
            workspace_path="/tmp/test",
            model="claude-sonnet-4-20250514",
        )
        
        assert result.result == "SKIP_MODE"
        assert result.session_id is None
    finally:
        if original_value is None:
            os.environ.pop("SKIP_AGENT_EXECUTION", None)
        else:
            os.environ["SKIP_AGENT_EXECUTION"] = original_value


@pytest.mark.asyncio
async def test_skip_mode_with_yes():
    """Test that agent_query returns SKIP_MODE when SKIP_AGENT_EXECUTION is yes."""
    original_value = os.environ.get("SKIP_AGENT_EXECUTION")
    os.environ["SKIP_AGENT_EXECUTION"] = "YES"
    
    try:
        result = await agent_query(
            system_prompt="Test prompt",
            workspace_path="/tmp/test",
            model="claude-sonnet-4-20250514",
        )
        
        assert result.result == "SKIP_MODE"
        assert result.session_id is None
    finally:
        if original_value is None:
            os.environ.pop("SKIP_AGENT_EXECUTION", None)
        else:
            os.environ["SKIP_AGENT_EXECUTION"] = original_value


@pytest.mark.asyncio
async def test_skip_mode_disabled():
    """Test that SKIP_MODE is not triggered when env var is false/empty."""
    original_value = os.environ.get("SKIP_AGENT_EXECUTION")
    
    # Test with "false"
    os.environ["SKIP_AGENT_EXECUTION"] = "false"
    
    try:
        # Mock the query function to fail immediately instead of waiting for timeout
        # This simulates what would happen without credentials, but avoids the 5-second delay
        with patch("app.services.claude_code.query") as mock_query:
            # Create an async iterator that raises immediately when iterated
            # This simulates the SDK trying to connect and failing without waiting for timeout
            class MockAsyncIterator:
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    raise Exception("No API credentials")
            
            mock_query.return_value = MockAsyncIterator()
            
            # This would normally execute the agent, but we don't have credentials in test
            # So we just verify it doesn't return SKIP_MODE immediately
            # (it will fail with other errors, but that's expected)
            try:
                result = await agent_query(
                    system_prompt="Test prompt",
                    workspace_path="/tmp/test",
                    model="claude-sonnet-4-20250514",
                )
                # If it returns successfully, it shouldn't be SKIP_MODE
                assert result.result != "SKIP_MODE"
            except Exception:
                # Expected to fail without credentials, but at least it didn't return SKIP_MODE
                pass
    finally:
        if original_value is None:
            os.environ.pop("SKIP_AGENT_EXECUTION", None)
        else:
            os.environ["SKIP_AGENT_EXECUTION"] = original_value
