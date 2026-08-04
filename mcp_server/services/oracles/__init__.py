"""SpecFlow oracles — the deterministic half of the refinement loop.

Orchestration is prose (skills spawning subagents); everything in this package
is code. The split is deliberate: an oracle's whole value is that it is not a
language model. "Check the state table is complete" as an instruction is
advisory. A script that exits non-zero on an empty cell is a forcing function.

The two halves ship through different channels, and that is what keeps the split
honest. Prose goes out as the marketplace plugin (``plugins/specflow``); code
goes out in this package, on PyPI as ``gd-specflow``. A skill reaches an oracle
only by running ``specflow refine ...``, so there is no way for prose to quietly
reimplement a check, and no way for a check to depend on a prompt.

Stdlib only. That is no longer forced — the CLI around it has real dependencies
— but these modules are pure functions over plain JSON, and keeping them that
way is what makes every verdict reproducible from the artifacts on disk.
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
