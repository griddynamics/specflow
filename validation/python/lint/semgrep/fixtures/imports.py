"""Fixtures for semgrep/imports.yml:
positive and negative cases per rule, one section per banned shape."""

import sys

# --- kit.python.import-fallback ---

# ruleid: kit.python.import-fallback
try:
    import ujson as fastjson
except ImportError:
    import json as fastjson

# ruleid: kit.python.import-fallback
try:
    from fastmarkdown import markdown
except ModuleNotFoundError:
    markdown = None

# ok: kit.python.import-fallback
import json

# ok: kit.python.import-fallback
try:
    connect()
except ImportError:
    raise RuntimeError("dependency missing") from None


# --- kit.python.sys-path-mutation ---

# ruleid: kit.python.sys-path-mutation
sys.path.insert(0, "/some/dir")

# ruleid: kit.python.sys-path-mutation
sys.path.append("/some/dir")

# ok: kit.python.sys-path-mutation
sys.exit(0)


# --- kit.python.package-dunder-probe ---

# ruleid: kit.python.package-dunder-probe
if __package__ is None:
    import script_helpers
else:
    from . import script_helpers

# ruleid: kit.python.package-dunder-probe
if __name__ == "__main__" and not __package__:
    import script_helpers

# ruleid: kit.python.package-dunder-probe
mod = __package__ or "default_pkg"

# ok: kit.python.package-dunder-probe
if __name__ == "__main__":
    main()


# --- kit.python.dynamic-import ---

# ruleid: kit.python.dynamic-import
spec = importlib.util.spec_from_file_location("mod", path)

# ruleid: kit.python.dynamic-import
mod = importlib.import_module("plugins.loader")

# ruleid: kit.python.dynamic-import
os_mod = __import__("os")

# ok: kit.python.dynamic-import
import os


# --- kit.python.module-feature-probe ---

import tomllib

...
# ruleid: kit.python.module-feature-probe
if hasattr(tomllib, "load"):
    tomllib.load(fh)

import yaml as pyyaml

...
# ruleid: kit.python.module-feature-probe
loader = getattr(pyyaml, "CSafeLoader", pyyaml.SafeLoader)

# ruleid: kit.python.module-feature-probe
parser = tomllib.loads if hasattr(tomllib, "loads") else fallback_parser


def parse_in_import_scope(value):
    # ruleid: kit.python.module-feature-probe
    if enabled and not hasattr(pyyaml, "CSafeLoader"):
        use(value)


# ok: kit.python.module-feature-probe
class Config:
    pass


config = Config()
if hasattr(config, "value"):
    use(config.value)

# ok: kit.python.module-feature-probe
stored = getattr(config, "value", None)

# ok: kit.python.module-feature-probe
# a dead-code-removal assertion: hasattr passed as a plain argument, not
# used as a guard/branch or a three-arg fallback -- not a probe.
from toolkit import toolchain

...
assertFalse(hasattr(toolchain, "_removed_hook"))

# ok: kit.python.module-feature-probe
# ordinary attribute use on an imported module -- no hasattr/getattr at all.
from toolkit import system

...
system.run_level("quick")


# --- kit.python.version-branch ---

# ruleid: kit.python.version-branch
if sys.version_info >= (3, 11):
    from tomllib import load
else:
    from tomli import load

# ruleid: kit.python.version-branch
if sys.version_info.major >= 3:  # `.major` attribute access, deep-expression coverage
    import tomllib
else:
    import tomli as tomllib

# ruleid: kit.python.version-branch
if sys.version_info[0] > 2:  # subscript comparison with `>`, deep-expression coverage
    import tomllib
else:
    import tomli as tomllib

# ok: kit.python.version-branch
running = f"{sys.version_info.major}.{sys.version_info.minor}"


# --- kit.python.no-type-checking ---

# ruleid: kit.python.no-type-checking
from typing import TYPE_CHECKING

# ruleid: kit.python.no-type-checking
if TYPE_CHECKING:
    pass

# ok: kit.python.no-type-checking
from typing import Any


# --- module runtime binding aliases ---
from domain.schemas import OrderSpec

# ruleid: kit.python.module-binding-alias
OldOrderSpec = OrderSpec

from domain import parse_order

# ruleid: kit.python.module-binding-alias
parse_old = parse_order


class CanonicalRecord:
    pass


# ruleid: kit.python.module-binding-alias
OldRecord = CanonicalRecord


def parse_record(value):
    return value


# ruleid: kit.python.module-binding-alias
old_parse = parse_record


async def load_record(value):
    return value


# ruleid: kit.python.module-binding-alias
old_load = load_record

from domain.constants import DEFAULT_LIMIT

# ok: kit.python.module-binding-alias
LIMIT = DEFAULT_LIMIT

# ok: kit.python.module-binding-alias
RecordId = TypeAliasType("RecordId", str)

# ok: kit.python.module-binding-alias
AuthDependency = Annotated[Account, Depends(current_user)]


# ok: kit.python.module-binding-alias
def local_binding():
    local_parse = parse_record
    return local_parse


class URL:
    pass


# ruleid: kit.python.module-binding-alias
OldURL = URL


# --- explicit adaptation declarations in comments ---
# ruleid: kit.python.explicit-compatibility-comment
# Backward compatibility for old parser callers.
# ruleid: kit.python.explicit-compatibility-comment
# This legacy alias will eventually be removed.

# ruleid: kit.python.explicit-compatibility-comment
# Kept for compatibility with former consumers.
# ruleid: kit.python.explicit-compatibility-comment
# Deprecated export still accepted by callers.

# ok: kit.python.explicit-compatibility-comment
# Legacy Office file formats are parsed and validated.
# ok: kit.python.explicit-compatibility-comment
# The failed command is kept for its retry notice.

# ok: kit.python.explicit-compatibility-comment
# A deprecated SDK function must never be called.


# ruleid: kit.python.import-fallback
try:
    prepare_environment()
    from domain.parser import parse
    configure(parse)
except ImportError as exc:
    parse = alternate_parse

# ruleid: kit.python.import-fallback
try:
    import optional_parser
except (ValueError, ModuleNotFoundError, ImportError):
    import alternate_parser
