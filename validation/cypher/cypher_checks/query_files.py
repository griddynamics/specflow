"""Query-file helpers every check shares: discovery, the stem binding and statement splitting.

A file's stem is its whole binding to a name in a policy register: `friends_of_friends.cypher`
binds to `Friends of friends`, `friends-of-friends` or `FRIENDS_OF_FRIENDS` alike.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path

__all__ = [
    "documented_name_for",
    "expand_targets",
    "glob_matches",
    "matches_any",
    "normalize_query_name",
    "split_statements",
    "statements_in",
]


def normalize_query_name(name: str) -> str:
    """A query name or file stem in the one comparable spelling.

    Backticks dropped, lower case, `_`/`-`/whitespace runs one space.
    """
    return re.sub(r"[\s_-]+", " ", name.replace("`", "")).strip().lower()


def documented_name_for(path: str | Path, names: Iterable[str]) -> str | None:
    """The name in `names` this file's stem binds to, or `None`.

    Names are tried in sorted order, so the answer never depends on the container's order.
    """
    stem = normalize_query_name(Path(path).stem)
    for name in sorted(names):
        if normalize_query_name(name) == stem:
            return name
    return None


@cache
def _glob_regex(glob: str) -> re.Pattern[str]:
    """`glob` as a regex: `**/` any directories (or none), `**` anything, `*` and `?` within
    one path segment."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))


def glob_matches(posix_path: str, glob: str) -> bool:
    """Whether a POSIX path matches `glob` in full, `**` crossing directories, `*` not."""
    return _glob_regex(glob).fullmatch(posix_path) is not None


def matches_any(path: str | Path, globs: Sequence[str]) -> bool:
    """Whether `path` (resolved, POSIX) or its bare file name matches any glob.

    `**/schema/*.cypher` matches by full path, `repoint_*.cypher` by file name.
    """
    candidate = Path(path)
    full = candidate.resolve().as_posix()
    return any(glob_matches(full, g) or glob_matches(candidate.name, g) for g in globs)


def expand_targets(targets: Iterable[str | Path]) -> list[Path]:
    """Every `.cypher` file named directly or found under a named directory, sorted."""
    found: set[Path] = set()
    for target in targets:
        path = Path(target)
        if path.is_dir():
            found.update(p for p in path.rglob("*.cypher") if p.is_file())
        else:
            found.add(path)
    return sorted(found)


def split_statements(text: str) -> tuple[str, ...]:
    """A DDL file's statements: `//` lines dropped, split on `;`, blanks removed."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return tuple(s.strip() for s in body.split(";") if s.strip())


def statements_in(path: Path, ddl_globs: Sequence[str]) -> list[str]:
    """Every statement in one `.cypher` file, as the server must receive them.

    A query file is one statement; a DDL file is split, since Neo4j takes one statement per
    call.
    """
    text = path.read_text(encoding="utf-8")
    if not matches_any(path, ddl_globs):
        return [text]
    return list(split_statements(text))
