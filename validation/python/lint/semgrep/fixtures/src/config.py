"""Fixture for semgrep/security.yml's `kit.security.runtime-tunable-literal` rule, the `ok`
half. Named and nested exactly as `src/config.py` so `paths.exclude` shields it -- this is
the one module allowed to carry the literal default a `Settings` field falls back to."""


def default_retries() -> int:
    # ok: kit.security.runtime-tunable-literal
    return build(retries=1)


def build(retries: int) -> int:
    return retries
