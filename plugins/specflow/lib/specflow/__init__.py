"""SpecFlow oracles — the deterministic half of the refinement loop.

Orchestration is prose (skills spawning subagents); everything in this package
is code. The split is deliberate: an oracle's whole value is that it is not a
language model. "Check the state table is complete" as an instruction is
advisory. A script that exits non-zero on an empty cell is a forcing function.

Stdlib only, by policy. This ships inside a marketplace plugin and runs on
whatever Python the user has; a ``pip install`` step turns a working skill into
a support ticket.

Entry point is ``../specflow_cli.py`` — deliberately outside this package, so
the package stays a pure library and the script owns the path bootstrap.
"""

from . import jsonschema_mini
from . import tree
from . import artifacts
from . import rank
from . import totality
from . import contracts
from . import concordance
from . import saturation
from . import mutate

__all__ = [
    "artifacts",
    "concordance",
    "contracts",
    "jsonschema_mini",
    "mutate",
    "rank",
    "saturation",
    "totality",
    "tree",
]

__version__ = "0.2.0"
