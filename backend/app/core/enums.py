"""
Core string enumerations for SpecFlow backend.

Values are byte-for-byte identical to the raw strings previously used in
environment variables and comparison sites so that existing env values and
== comparisons continue to work without change.
"""

from enum import StrEnum


class LLMProvider(StrEnum):
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"


class AuthMode(StrEnum):
    API_KEY = "api_key"
    LOCAL = "local"


class DatabaseType(StrEnum):
    MEMORY = "memory"
    EMULATOR = "emulator"
    FIRESTORE = "firestore"
    SQLITE = "sqlite"
