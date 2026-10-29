# Backend guidance

Run backend commands from `backend/` (or from the repo root through the root
`Makefile`, which delegates here). The environment file is `../.env.local`, for
example `uv run --env-file ../.env.local examples/<example>.py`.

## Python imports

Use package-qualified absolute imports for internal modules, such as
`from llm.agent import Agent`. Do not use relative imports or bare internal
module imports. Top-level service modules (`cli`, `health`, `settings`) are
imported by module name, for example `from settings import load_settings`.

## Logging

When adding logging to any feature, use the central `logger.logger.get_logger`/`log_event`; no stdlib `logging`
setup, no diagnostic `print()`.
