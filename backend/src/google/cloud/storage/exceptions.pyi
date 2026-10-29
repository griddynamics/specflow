"""Type stub for ``google.cloud.storage.exceptions`` (see ``__init__.pyi`` for why it exists)."""

class InvalidPathError(Exception): ...

class InvalidResponse(Exception):
    response: object
    def __init__(self, response: object, *args: object) -> None: ...

class DataCorruption(Exception):
    response: object
    def __init__(self, response: object, *args: object) -> None: ...
