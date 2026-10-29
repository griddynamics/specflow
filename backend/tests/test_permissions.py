from __future__ import annotations

import pytest
from claude_agent_sdk import AgentDefinition

from llm.permissions import AgentPermissions, validate_subagent_models


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Glob", "Bash"])
def test_unconstrained_file_and_shell_tools_are_rejected(tool: str) -> None:
    with pytest.raises(ValueError, match=f"unsafe: {tool}"):
        AgentPermissions(allowed_tools=(tool,))


def test_permission_error_explains_scoping_and_explicit_override() -> None:
    with pytest.raises(ValueError, match="allow_unconstrained_tools") as exc_info:
        AgentPermissions(allowed_tools=("Bash", "Write"))

    message = str(exc_info.value)
    assert "Read(//workspace/**)" in message
    assert "Bash(git status:*)" in message
    assert "allow_unconstrained_tools=True" in message


def test_scoped_and_non_privileged_tool_grants_are_accepted() -> None:
    permissions = AgentPermissions(
        allowed_tools=(
            "Read(//workspace/**)",
            "Edit(//workspace/**)",
            "Glob(//workspace/**)",
            "Bash(git status:*)",
            "WebSearch",
        ),
        disallowed_tools=("Bash(rm:*)",),
        permission_mode="acceptEdits",
    )

    assert permissions.permission_mode == "acceptEdits"


def test_scoped_write_rule_explains_supported_edit_replacement() -> None:
    with pytest.raises(ValueError, match=r"Use Edit\(path\)") as exc_info:
        AgentPermissions(allowed_tools=("Write(/workspace/**)",))

    assert "unsupported scoped rules" in str(exc_info.value)


def test_unconstrained_tools_require_explicit_opt_in() -> None:
    permissions = AgentPermissions(
        allowed_tools=("Bash",),
        allow_unconstrained_tools=True,
    )

    assert permissions.allowed_tools == ("Bash",)


@pytest.mark.parametrize("tool", ["", " Read", "Write "])
def test_invalid_tool_names_are_rejected(tool: str) -> None:
    with pytest.raises(ValueError, match="allowed_tools entries"):
        AgentPermissions(allowed_tools=(tool,))


def test_subagents_may_inherit_or_match_parent_model() -> None:
    subagents = {
        "implicit": AgentDefinition(description="d", prompt="p"),
        "inherit": AgentDefinition(
            description="d",
            prompt="p",
            model="inherit",
        ),
        "matching": AgentDefinition(
            description="d",
            prompt="p",
            model="provider/model",
        ),
    }

    validate_subagent_models(
        "provider/model",
        subagents,
        allow_mismatch=False,
    )


def test_subagent_model_mismatch_has_actionable_diagnostic() -> None:
    subagents = {
        "reviewer": AgentDefinition(
            description="d",
            prompt="p",
            model="different/model",
        )
    }

    with pytest.raises(ValueError, match="allow_subagent_model_mismatch") as exc_info:
        validate_subagent_models(
            "parent/model",
            subagents,
            allow_mismatch=False,
        )

    message = str(exc_info.value)
    assert "reviewer='different/model'" in message
    assert "parent/model" in message
    assert "allow_subagent_model_mismatch=True" in message


def test_subagent_model_mismatch_can_be_explicitly_allowed() -> None:
    validate_subagent_models(
        "parent/model",
        {
            "reviewer": AgentDefinition(
                description="d",
                prompt="p",
                model="different/model",
            )
        },
        allow_mismatch=True,
    )
