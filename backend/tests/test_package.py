from __future__ import annotations

import asyncio
from importlib.resources import files

import pytest

import llm as package
import llm.code_factory_lib as code_factory
from llm import evidence, file_formats, judge, tracing


@pytest.mark.asyncio
async def test_package_imports() -> None:
    await asyncio.sleep(0)

    assert package.__name__ == "llm"
    assert package.CodingAgent.__name__ == "CodingAgent"
    assert callable(package.conservative_bash_timeout_environment)
    assert package.AgentSettings.__name__ == "AgentSettings"
    assert package.AgentPermissions.__name__ == "AgentPermissions"
    assert package.PromptSequence.__name__ == "PromptSequence"
    assert package.PromptStep.__name__ == "PromptStep"
    assert package.StepRunner.__name__ == "StepRunner"
    assert package.AgentDAG.__name__ == "AgentDAG"
    assert package.AgentNode.__name__ == "AgentNode"
    assert package.Decision.__name__ == "Decision"
    assert package.DAGResult.__name__ == "DAGResult"
    assert repr(package.END) == "END"
    assert callable(package.build_validated_agent_dag)
    assert package.ValidatedRunContext.__name__ == "ValidatedRunContext"
    assert package.CrashBackoffPolicy.__name__ == "CrashBackoffPolicy"
    assert package.GitHeadProgressDetector.__name__ == "GitHeadProgressDetector"
    assert package.NativeSandboxPolicy.__name__ == "NativeSandboxPolicy"
    assert package.SandboxUnavailableError.__name__ == "SandboxUnavailableError"
    assert callable(package.check_native_sandbox_available)
    assert callable(code_factory.run_code_factory)
    assert callable(evidence.run_command)
    assert callable(evidence.ensure_git_workspace)
    assert callable(judge.run_structured_judge)
    assert callable(file_formats.parse_markdown_plan)
    assert tracing.LangfuseMessageObserver.__name__ == "LangfuseMessageObserver"
    assert tracing.LangfuseTraceRegistry.__name__ == "LangfuseTraceRegistry"
    assert tracing.LangfuseRegistryEvidence.__name__ == "LangfuseRegistryEvidence"
    assert files(package).joinpath("py.typed").is_file()
