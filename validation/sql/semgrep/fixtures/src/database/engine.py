"""Real-scan fixture for sql.yml's `kit.sql.self-opened-engine-outside-database-module`,
the `ok` half: nested under src/database/ so `paths.exclude` shields it."""


def build_engine(url: str, *, busy_timeout_s: int) -> object:
    # ok: kit.sql.self-opened-engine-outside-database-module
    return create_async_engine(url, connect_args={"timeout": busy_timeout_s})


def create_async_engine(url: str, **kwargs: object) -> object: ...
