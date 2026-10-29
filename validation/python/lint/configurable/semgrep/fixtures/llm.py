"""Fixtures for llm.yml, paired with it by stem for `semgrep --test` (which ignores `paths:`).

`Agent` and `llm_sdk` are local stand-ins, so the fixture needs no installed client library."""

from __future__ import annotations

# --- kit.configurable.llm-inline-prompt-string ---


def build_agent_inline() -> object:
    # ruleid: kit.configurable.llm-inline-prompt-string
    return Agent(system_prompt="You summarise an order history in one short paragraph.")


def build_agent_inline_instructions() -> object:
    # ruleid: kit.configurable.llm-inline-prompt-string
    return Agent(instructions="Classify the support ticket by its main topic.")


def run_agent_inline(agent: object, settings: object) -> object:
    # ruleid: kit.configurable.llm-inline-prompt-string
    return agent.run("List every open order of this customer by date.", timeout=settings.timeout)


def build_agent_from_file(prompt_text: str) -> object:
    # ok: kit.configurable.llm-inline-prompt-string
    return Agent(system_prompt=prompt_text)


def build_agent_short_literal() -> object:
    # ok: kit.configurable.llm-inline-prompt-string
    return Agent(system_prompt="Be brief.")


class Agent:
    def __init__(self, system_prompt: str | None = None, instructions: str | None = None) -> None:
        self.system_prompt = system_prompt or instructions


# --- kit.configurable.llm-model-id-literal-outside-settings ---


def default_model() -> str:
    # ruleid: kit.configurable.llm-model-id-literal-outside-settings
    return "gpt-4o-mini"


def default_embedding_model() -> str:
    # ruleid: kit.configurable.llm-model-id-literal-outside-settings
    return "text-embedding-3-small"


def configured_model(settings: object) -> str:
    # ok: kit.configurable.llm-model-id-literal-outside-settings
    return settings.chat_model


# --- kit.configurable.llm-run-missing-timeout ---


async def call_without_timeout(agent: object, prompt: str) -> object:
    # ruleid: kit.configurable.llm-run-missing-timeout
    return await agent.run(prompt)


async def call_with_timeout(agent: object, prompt: str, settings: object) -> object:
    # ok: kit.configurable.llm-run-missing-timeout
    return await agent.run(prompt, timeout=settings.timeout)


# --- kit.configurable.llm-temperature-literal ---


def build_agent_literal_temperature() -> object:
    # ruleid: kit.configurable.llm-temperature-literal
    return build_typed_agent(temperature=0.7)


def build_agent_from_settings(settings: object) -> object:
    # ok: kit.configurable.llm-temperature-literal
    return build_typed_agent(temperature=settings.temperature)


def build_typed_agent(*, temperature: float = 0.0) -> object: ...


# --- kit.configurable.llm-client-not-from-settings ---


def build_client_literal() -> object:
    # ruleid: kit.configurable.llm-client-not-from-settings
    return llm_sdk.Client(api_key="hardcoded-key-value")


def build_client_from_settings(settings: object) -> object:
    # ok: kit.configurable.llm-client-not-from-settings
    return llm_sdk.Client(api_key=settings.llm_api_key)


def build_client_from_options(options: dict[str, str]) -> object:
    # ok: kit.configurable.llm-client-not-from-settings
    return llm_sdk.Client(**options)


# --- kit.configurable.llm-no-file-log ---


def build_file_logger() -> object:
    # ruleid: kit.configurable.llm-no-file-log
    return FileTrafficLogger("traffic.log")


def read_log_path(settings: object) -> object:
    # ruleid: kit.configurable.llm-no-file-log
    return settings.traffic_log_path


def build_structured_logger(logger: object) -> object:
    # ok: kit.configurable.llm-no-file-log
    return logger


class FileTrafficLogger:
    def __init__(self, path: str) -> None:
        self.path = path


# Stands in for the provider module.
class llm_sdk:
    class Client:
        def __init__(self, api_key: str) -> None: ...
