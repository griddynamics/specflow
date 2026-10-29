"""Static `.cypher` lint: the rules a live database cannot see, read off the query text alone.

Line- and regex-based per rule, one `_*_findings` function each. Every schema-dependent rule
reads the `GraphSchema` bound by `check`; nothing about any one graph lives here.
Run as `python -m cypher_checks lint --schema schema.yaml <file-or-dir> [...]`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from cypher_checks.findings import Finding, sort_key
from cypher_checks.query_files import (
    documented_name_for,
    expand_targets,
    matches_any,
    normalize_query_name,
)
from cypher_checks.schema import CappedTraversal, GraphSchema

__all__ = ["DOCS", "check", "check_file", "code_text", "read_register_findings", "using_schema"]

DOC_BASE = "RULES.md"
DOC_BOUND = f"{DOC_BASE}#bound-every-variable-length-path"
DOC_MERGE = f"{DOC_BASE}#merge-needs-a-full-key"
DOC_CREATE_DELETE = f"{DOC_BASE}#no-data-create-no-node-delete"
DOC_WRITE_ONLY_WHERE_ALLOWED = f"{DOC_BASE}#write-only-where-allowed"
DOC_ANCHOR = f"{DOC_BASE}#anchor-first-expand-second"
DOC_PARAMETERIZE = f"{DOC_BASE}#parameterize-everything"
DOC_QUERY_FILES = f"{DOC_BASE}#one-statement-per-file"
DOC_FORMATTING = f"{DOC_BASE}#formatting"
DOC_SHARED_SET = f"{DOC_BASE}#set-shared-properties-once"
DOC_SCHEMA_VOCABULARY = f"{DOC_BASE}#schema-vocabulary"
DOC_RETURN_ONLY = f"{DOC_BASE}#return-only-what-the-caller-uses"
DOC_PRUNE = f"{DOC_BASE}#deterministic-cuts"
DOC_READ_OWNERS = f"{DOC_BASE}#every-read-has-an-owner"
#: Every anchor a finding may cite; the kit's own documentation must define each one.
DOCS = (
    DOC_READ_OWNERS,
    DOC_BOUND,
    DOC_MERGE,
    DOC_CREATE_DELETE,
    DOC_WRITE_ONLY_WHERE_ALLOWED,
    DOC_ANCHOR,
    DOC_PARAMETERIZE,
    DOC_QUERY_FILES,
    DOC_FORMATTING,
    DOC_SHARED_SET,
    DOC_SCHEMA_VOCABULARY,
    DOC_RETURN_ONLY,
    DOC_PRUNE,
)

WRITE_CLAUSE_RE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|FOREACH)\b")
CLAUSE_KEYWORDS = (
    "MATCH",
    "OPTIONAL MATCH",
    "MERGE",
    "CREATE",
    "SET",
    "DELETE",
    "DETACH DELETE",
    "REMOVE",
    "WHERE",
    "RETURN",
    "WITH",
    "UNWIND",
    "CALL",
    "FOREACH",
    "ORDER BY",
)
_LENGTH_FILTER_RE = re.compile(r"length\(\s*\w+\s*\)\s*<=\s*\$(\w+)")
# The map, if any, is found by the balanced `_map_span_at` scan: `re` cannot express nested
# braces. The label group repeats so `MERGE (m:Movie:Entity {id: $id})` matches up
# to the property map, not the second label.
_MERGE_NODE_RE = re.compile(r"MERGE\s*\(\s*\w*\s*:\s*(\w+)((?:\s*:\s*\w+)*)\s*")
_PROP_KEY_RE = re.compile(r"(\w+)\s*:")
# Every relationship type named in a pattern, as rule 6 reads them. `types` is the whole type
# expression after `:` (one name or a `|` alternation); it stops before a quantifier, a map,
# a `WHERE` or the closing `]`.
_REL_TYPE_EXPR_RE = re.compile(
    r"\[\s*\w*\s*:\s*(?P<types>[A-Za-z_]\w*(?:\s*\|\s*:?\s*[A-Za-z_]\w*)*)"
)
_TEMPLATING_RES = (
    re.compile(r"\{\{"),
    re.compile(r"%s"),
    re.compile(r"apoc\.cypher\.run"),
    re.compile(r"\(\s*\w*\s*:\s*\$"),  # a dynamic label expression: (n:$label)
)


_SCHEMA: ContextVar[GraphSchema] = ContextVar("cypher_lint_schema")


@contextmanager
def using_schema(schema: GraphSchema) -> Iterator[None]:
    """Bind `schema` for every rule run inside the block."""
    token = _SCHEMA.set(schema)
    try:
        yield
    finally:
        _SCHEMA.reset(token)


def _schema() -> GraphSchema:
    return _SCHEMA.get()


def _capped_for(path: Path) -> CappedTraversal | None:
    """The `policy.capped_traversals` row this file's stem binds to, or `None`."""
    return _schema().policy.capped_for(path)


def _is_scan_shaped(path: Path) -> bool:
    """Whether `policy.scan_shaped_queries` names this file -- the anchor rule's one exemption."""
    return _schema().policy.is_scan_shaped(path)


def _is_ddl_file(path: Path) -> bool:
    """A DDL file (`policy.ddl_globs`): many statements, `CREATE` its verb, no data rules."""
    return matches_any(path, _schema().policy.ddl_globs)


def _is_write_file(path: Path) -> bool:
    return matches_any(path, _schema().policy.write_globs)


def _blank_span(text: str, start: int, end: int) -> str:
    """`text[start:end]` with every character but `\\n` replaced by a space,
    so the caller's line/column numbers stay identical to the original."""
    return "".join(ch if ch == "\n" else " " for ch in text[start:end])


def _blank_comments(text: str) -> str:
    """Cypher's two comment forms -- `// to end of line` and `/* ... */`
    (which may span lines) -- blanked to spaces. `--` is deliberately NOT a
    Cypher comment marker: it is two path-arrow dashes (`-->(m:Movie)`) and
    must never be blanked, unlike SQL's `--`."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(_blank_span(text, i, end))
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(_blank_span(text, i, end))
            i = end
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _blank_quoted_strings(text: str, quote: str) -> str:
    """Every `quote`-delimited literal's interior blanked to spaces, quotes kept.

    A doubled quote is an escape. A keyword or colon inside a value then never trips a rule,
    and a map's shape (`{id: 'x'}` vs `{id: $id}`) stays visible."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == quote:
            j = i + 1
            closed = False
            while j < n:
                if text[j] == quote and text[j + 1 : j + 2] == quote:
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    closed = True
                    break
                j += 1
            if closed:
                out.append(quote + _blank_span(text, i + 1, j - 1) + quote)
            else:
                out.append(text[i:j])  # unterminated string reaching EOF -- left as-is
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _blank_single_quoted_strings(text: str) -> str:
    return _blank_quoted_strings(text, "'")


def _blank_double_quoted_strings(text: str) -> str:
    """Double-quoted interiors blanked too, so `{k: "a: b"}` yields no second map key.

    Cypher quotes strings either way; `check_file` chains this after the single-quote pass."""
    return _blank_quoted_strings(text, '"')


def code_text(text: str) -> str:
    """`text` as the rules read it: comments and both kinds of string interior blanked."""
    return _blank_double_quoted_strings(_blank_single_quoted_strings(_blank_comments(text)))


def _balanced_map_end(text: str, open_pos: int) -> int | None:
    """The index one past the `}` matching the `{` at `text[open_pos]`, or `None` if unclosed.

    A brace-depth count, so nested constructor maps (`datetime({epochMillis: $ms})`) close
    correctly. Safe because callers pass `code`, whose string interiors are already blanked;
    `_map_span_at` has checked `text[open_pos] == "{"`."""
    depth = 0
    i, n = open_pos, len(text)
    while i < n:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _map_span_at(text: str, pos: int) -> tuple[int, int] | None:
    """`(start, end)` of the `{...}` map beginning exactly at `text[pos]`
    (skipping no characters -- callers position `pos` themselves), balanced
    per `_balanced_map_end`, or `None` if `pos` is out of range, not a `{`,
    or the map never closes."""
    if pos >= len(text) or text[pos] != "{":
        return None
    end = _balanced_map_end(text, pos)
    return None if end is None else (pos, end)


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _top_level_prop_keys(text: str, start: int, end: int) -> list[re.Match[str]]:
    """`_PROP_KEY_RE` matches in `text[start:end]` whose `:` sits at brace depth 1.

    Those are keys of this map only, never of a constructor map nested in a value."""
    depths: list[int] = []
    depth = 0
    for ch in text[start:end]:
        if ch == "{":
            depth += 1
            depths.append(depth)
        elif ch == "}":
            depths.append(depth)
            depth -= 1
        else:
            depths.append(depth)
    return [km for km in _PROP_KEY_RE.finditer(text, start, end) if depths[km.start() - start] == 1]


# --- 1. Bound every variable-length path -----------------------------------

# A relationship `*` quantifier or a quantified path pattern's `){min,max}`, the two
# variable-length spellings. Five `*` alternatives keep "open" apart from "exact":
#   `*]` and `*n..]` are unbounded (`hi1` present but empty for the latter);
#   `*n]` is bounded by `n` (`exact`); `*..m]` and `*n..m]` by `m` (`hi2`, `hi1`).
_REL_QUANTIFIER_RE = re.compile(
    r"\*(?:(?P<lo1>\d+)\.\.(?P<hi1>\d*)|\.\.(?P<hi2>\d+)|(?P<exact>\d+))?\s*\]"
)
# A quantified path pattern's `){lo,hi}` / `){lo,}` (open) / `){,hi}` / `){n}` / `)+` / `)*`.
_QPP_QUANTIFIER_RE = re.compile(r"\)\s*\{\s*(?P<lo>\d*)\s*,?\s*(?P<hi>\d*)\s*\}|\)(?P<star>[\+\*])")


def _rel_quantifier_upper(m: re.Match[str]) -> str | None:
    if m.group("hi1") is not None:
        return m.group("hi1") or None  # "" (open upper, e.g. *2..) is unbounded
    if m.group("hi2") is not None:
        return m.group("hi2")
    return m.group("exact")  # None for a bare "*" (unbounded); the count itself otherwise


def _qpp_quantifier_upper(m: re.Match[str]) -> str | None:
    if m.group("star") is not None:
        return None
    has_comma = "," in m.group(0)
    if not has_comma:
        return m.group("lo") or None  # "{n}": an exact count, bounded by n itself
    return m.group("hi") or None  # "{lo,hi}" bounded, "{lo,}"/"{,}" open (unbounded)


def _quantifier_matches(line: str) -> list[tuple[re.Match[str], str | None]]:
    """Every quantifier on `line` as `(match, upper_bound_or_None)`."""
    out: list[tuple[re.Match[str], str | None]] = [
        (m, _rel_quantifier_upper(m)) for m in _REL_QUANTIFIER_RE.finditer(line)
    ]
    out += [(m, _qpp_quantifier_upper(m)) for m in _QPP_QUANTIFIER_RE.finditer(line)]
    return out


def _bound_findings(rel: str, path: Path, code: str) -> list[Finding]:
    # The cap and key come from the policy's capped-traversal register; a file it does not
    # name gets neither cap rule.
    capped = _capped_for(path)
    title = capped.query if capped is not None else None
    findings = []
    for lineno, line in enumerate(code.splitlines(), start=1):
        for match, upper in _quantifier_matches(line):
            if not upper:
                findings.append(
                    Finding(
                        rel,
                        lineno,
                        match.start() + 1,
                        "cypher-lint-unbounded-path",
                        "variable-length path has no upper bound",
                        fix="add a literal upper bound, e.g. *1..N",
                        doc=DOC_BOUND,
                    )
                )
                continue
            if capped is not None and int(upper) > capped.hard_cap:
                findings.append(
                    Finding(
                        rel,
                        lineno,
                        match.start() + 1,
                        "cypher-lint-hard-cap-exceeded",
                        f"upper bound {upper} exceeds the registered hard cap "
                        f"{capped.hard_cap} for {title!r}",
                        fix="lower the literal bound to match the registered cap",
                        doc=DOC_BOUND,
                    )
                )
    has_bounded_quantifier = _REL_QUANTIFIER_RE.search(code) or _QPP_QUANTIFIER_RE.search(code)
    if has_bounded_quantifier and not _LENGTH_FILTER_RE.search(code):
        findings.append(
            Finding(
                rel,
                1,
                1,
                "cypher-lint-missing-length-filter",
                "a bounded variable-length path has no length(p) <= $key filter",
                fix="add WHERE length(p) <= $<config_key>",
                doc=DOC_BOUND,
            )
        )
    elif capped is not None:
        keys = {m.group(1) for m in _LENGTH_FILTER_RE.finditer(code)}
        # A config key is often UPPER_SNAKE while a `$param` is lower_snake, so compare
        # case-insensitively.
        if keys and str(capped.key).lower() not in {k.lower() for k in keys}:
            findings.append(
                Finding(
                    rel,
                    1,
                    1,
                    "cypher-lint-wrong-config-key",
                    f"length() filter uses {sorted(keys)}, not the registered "
                    f"config key {capped.key!r}",
                    fix=f"filter on $'{capped.key}'",
                    doc=DOC_BOUND,
                )
            )
    return findings


# --- 2. MERGE needs a full key ----------------------------------------------


def _direct_parameter_row_key(prefix: str, props: str, key: str) -> bool:
    """Recognize an immediately preceding direct parameter UNWIND, without rebinding."""
    binding = re.fullmatch(
        r"\s*UNWIND\s+\$[A-Za-z_]\w*\s+AS\s+([A-Za-z_]\w*)\s*",
        prefix,
        flags=re.IGNORECASE,
    )
    if binding is None:
        return False
    return (
        re.fullmatch(
            rf"\{{\s*{re.escape(key)}\s*:\s*{re.escape(binding.group(1))}\.[A-Za-z_]\w*\s*\}}",
            props,
        )
        is not None
    )


_PARAMETER_RE = re.compile(r"\$[A-Za-z_]\w*")
# `UNWIND $rows AS row` or `UNWIND row.items AS item`: the source and the variable it binds.
_UNWIND_BINDING_RE = re.compile(
    r"\bUNWIND\s+(\$[A-Za-z_]\w*|[A-Za-z_]\w*\.[A-Za-z_]\w*)\s+AS\s+([A-Za-z_]\w*)",
    re.IGNORECASE,
)


def _merge_key_entries(
    code: str, span: tuple[int, int]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The `(key, value)` entries of a `MERGE` map, and the `policy.merge_scope_keys` ones.

    Scope keys are set aside only beside another key: a map of scope keys alone keeps them,
    so it is judged as any other map."""
    matches = _top_level_prop_keys(code, *span)
    ends = [km.start() for km in matches[1:]] + [span[1] - 1] if matches else []
    entries = [
        (km.group(1), code[km.end() : end].strip().rstrip(",").strip())
        for km, end in zip(matches, ends, strict=True)
    ]
    scope = set(_schema().policy.merge_scope_keys)
    own = [entry for entry in entries if entry[0] not in scope]
    if not own:
        return entries, []
    return own, [entry for entry in entries if entry[0] in scope]


def _batch_row_key(prefix: str, code: str, value: str) -> bool:
    """`value` is `row.prop` of a row an earlier `UNWIND` binds from a `$parameter` (or from a
    list property of such a row), and the file binds that variable nowhere else.

    Only `policy.batch_merge_queries` files ask: no expression and no `WITH ... AS` rebinding
    may invent the key."""
    key = re.fullmatch(r"([A-Za-z_]\w*)\.[A-Za-z_]\w*", value)
    if key is None:
        return False
    rows: set[str] = set()
    for binding in _UNWIND_BINDING_RE.finditer(prefix):
        source, var = binding.groups()
        if source.startswith("$") or source.split(".", 1)[0] in rows:
            rows.add(var)
        else:
            rows.discard(var)
    var = key.group(1)
    if var not in rows:
        return False
    unwinds = sum(1 for b in _UNWIND_BINDING_RE.finditer(code) if b.group(2) == var)
    bindings = len(re.findall(rf"\bAS\s+{re.escape(var)}\b", code, re.IGNORECASE))
    return bindings == unwinds


def _merge_key_findings(rel: str, path: Path, code: str) -> list[Finding]:
    """Node `MERGE`s key on exactly one `$parameter` property, read over the blanked text.

    The one key must also be uniquely constrained on a label the pattern names
    (`cypher-lint-merge-key-label-unconstrained`); DDL files are exempt.

    Keys come from the balanced `_map_span_at`/`_top_level_prop_keys` scan, so nested maps
    and quoted colons add none; an empty `{}` reports `cypher-lint-merge-no-key`."""
    findings: list[Finding] = []
    if _is_ddl_file(path):
        return findings
    unique = set(_schema().unique)
    for match in _MERGE_NODE_RE.finditer(code):
        lineno = _line_of(code, match.start())
        col = match.start() - code.rfind("\n", 0, match.start())
        label = match.group(1)
        span = _map_span_at(code, _skip_space(code, match.end()))
        # No map (`MERGE (b:Movie)`) and an empty one (`{}`) both mean "nothing to key on",
        # so this branch narrows `span` to non-`None` for the rest without an `assert`.
        if span is None:
            findings.append(
                Finding(
                    rel,
                    lineno,
                    col,
                    "cypher-lint-merge-no-key",
                    f"MERGE ({label}) has no property map to key on",
                    fix="MERGE on exactly one uniquely constrained property, as a $parameter",
                    doc=DOC_MERGE,
                )
            )
            continue
        entries, scoped = _merge_key_entries(code, span)
        keys = [key for key, _ in entries]
        findings += [
            Finding(
                rel,
                lineno,
                col,
                "cypher-lint-merge-key-not-parameter",
                f"MERGE scope key {key!r} is not a $parameter",
                fix="pass the key as a $parameter, never a literal",
                doc=DOC_MERGE,
            )
            for key, value in scoped
            if _PARAMETER_RE.fullmatch(value) is None
        ]
        if not keys:
            findings.append(
                Finding(
                    rel,
                    lineno,
                    col,
                    "cypher-lint-merge-no-key",
                    f"MERGE ({label}) has no property map to key on",
                    fix="MERGE on exactly one uniquely constrained property, as a $parameter",
                    doc=DOC_MERGE,
                )
            )
            continue
        if len(keys) != 1:
            findings.append(
                Finding(
                    rel,
                    lineno,
                    col,
                    "cypher-lint-merge-multiple-keys",
                    f"MERGE ({label} {{{', '.join(keys)}}}) keys on {len(keys)} "
                    "properties, not one",
                    fix="MERGE on exactly one property; SET the rest afterwards",
                    doc=DOC_MERGE,
                )
            )
            continue
        # With scope keys set aside, the key is judged as if it were the map's only entry.
        props = f"{{{keys[0]}: {entries[0][1]}}}" if scoped else code[span[0] : span[1]]
        if (
            f"{keys[0]}: $" not in props
            and f"{keys[0]}:$" not in props
            and not _direct_parameter_row_key(code[: match.start()], props, keys[0])
            and not (
                _schema().policy.is_batch_merge(path)
                and _batch_row_key(code[: match.start()], code, entries[0][1])
            )
        ):
            findings.append(
                Finding(
                    rel,
                    lineno,
                    col,
                    "cypher-lint-merge-key-not-parameter",
                    f"MERGE key {keys[0]!r} is not a $parameter",
                    fix="pass the key as a $parameter, never a literal",
                    doc=DOC_MERGE,
                )
            )
        labels = [label, *re.findall(r"\w+", match.group(2))]
        if unique and not any((name, keys[0]) in unique for name in labels):
            findings.append(
                Finding(
                    rel,
                    lineno,
                    col,
                    "cypher-lint-merge-key-label-unconstrained",
                    f"MERGE ({':'.join(labels)}) keys on {keys[0]!r}, which no label of the "
                    "pattern holds a uniqueness constraint on",
                    fix="add the label that carries the constraint on this property",
                    doc=DOC_MERGE,
                )
            )
    return findings


# --- 3. No CREATE / node DELETE for data ------------------------------------

# Anywhere on the line, word-boundary only -- `WITH n CREATE (...)` must fire
# just as much as a line that starts with the keyword.
_CREATE_RE = re.compile(r"\bCREATE\b", re.IGNORECASE)
# `MERGE ... ON CREATE SET ...` / `ON MATCH SET ...` are MERGE's own
# conditional sub-clauses, not a data CREATE -- a CREATE inside one is exempt.
_ON_CREATE_RE = re.compile(r"\bON\s+CREATE\b", re.IGNORECASE)
_DELETE_RE = re.compile(r"\b(?:DETACH\s+)?DELETE\b", re.IGNORECASE)
# The relationship pattern a re-point's own CREATE writes: `(a)-[r:T]->(b)`
# (either arrow direction). A node-creating CREATE has no `[...]` after it and
# is never covered by the allowance below.
_CREATE_RELATIONSHIP_RE = re.compile(r"<?-\s*\[")
_ELEMENT_ID_RE = re.compile(r"\belementId\s*\(", re.IGNORECASE)
# What a `DELETE`/`DETACH DELETE` clause's target-variable list stops at:
# the next clause keyword, so "DELETE r RETURN r" doesn't pull "RETURN r"
# into the target list.
_DELETE_BOUNDARY_RE = re.compile(
    r"\b(?:RETURN|WITH|SET|MATCH|MERGE|WHERE|CREATE|UNWIND|CALL)\b", re.IGNORECASE
)


def _delete_target_vars(line: str, after: int) -> list[str]:
    rest = line[after:]
    boundary = _DELETE_BOUNDARY_RE.search(rest)
    if boundary is not None:
        rest = rest[: boundary.start()]
    return [tok.strip() for tok in rest.split(",") if tok.strip()]


def _is_relationship_var(code: str, var: str) -> bool:
    """Whether `var` is ever bound inside a relationship pattern's `[...]`
    (`[r:TYPE]`, `[r]`) anywhere in the file -- a node pattern only ever
    binds its variable inside `(...)`, never `[...]`."""
    if not var.isidentifier():
        return False
    return re.search(rf"\[\s*{re.escape(var)}\s*(?::|\])", code) is not None


def _is_repointed_create(path: Path, code: str, line: str, after: int) -> bool:
    """Whether this `CREATE` is the one edge re-point allowance.

    `policy.create_allowed_globs` names the file, it writes an edge rather than a node, and it
    takes the original edge off: matched by `elementId` and deleted, or bound in a relationship
    pattern of the file (every edge of the merged node at once) and deleted by that variable.
    All of it holds, or it stays a finding.
    """
    if not matches_any(path, _schema().policy.create_allowed_globs):
        return False
    if not _CREATE_RELATIONSHIP_RE.search(line[after:]):
        return False
    if _ELEMENT_ID_RE.search(code) and _DELETE_RE.search(code):
        return True
    return _deletes_a_matched_relationship(code)


def _deletes_a_matched_relationship(code: str) -> bool:
    """Whether a plain `DELETE` of this file targets a variable it binds in a `[...]` pattern."""
    for line in code.splitlines():
        for match in _DELETE_RE.finditer(line):
            if "DETACH" in match.group(0).upper():
                continue
            if any(_is_relationship_var(code, t) for t in _delete_target_vars(line, match.end())):
                return True
    return False


def _no_create_or_delete_findings(rel: str, path: Path, code: str) -> list[Finding]:
    if _is_ddl_file(path):
        return []
    findings = []
    for lineno, line in enumerate(code.splitlines(), start=1):
        on_create_spans = [m.span() for m in _ON_CREATE_RE.finditer(line)]
        for match in _CREATE_RE.finditer(line):
            if any(s <= match.start() and match.end() <= e for s, e in on_create_spans):
                continue  # part of MERGE's own "ON CREATE SET ...", not a data CREATE
            if _is_repointed_create(path, code, line, match.end()):
                continue  # the one registered allowance (policy.create_allowed_globs)
            findings.append(
                Finding(
                    rel,
                    lineno,
                    match.start() + 1,
                    "cypher-lint-create-not-allowed",
                    "CREATE is never used for data outside a registered edge re-point",
                    fix="use MERGE -- the one allowance is an edge re-point file "
                    "policy.create_allowed_globs names",
                    doc=DOC_CREATE_DELETE,
                )
            )
        for match in _DELETE_RE.finditer(line):
            is_detach = "DETACH" in match.group(0).upper()
            targets = _delete_target_vars(line, match.end())
            # DETACH always targets a node (relationships have no detach semantics); a bare
            # DELETE targets a node unless every deleted variable is one this file itself
            # binds inside a `[...]` relationship pattern.
            deletes_a_node = (
                is_detach or not targets or any(not _is_relationship_var(code, t) for t in targets)
            )
            if deletes_a_node and not _deletes_only_deletable_labels(path, code, targets):
                findings.append(
                    Finding(
                        rel,
                        lineno,
                        match.start() + 1,
                        "cypher-lint-delete-not-allowed",
                        "nodes are deactivated, never DELETEd",
                        fix="SET a deactivation flag instead of DELETE, or register the file "
                        "under policy.node_removals",
                        doc=DOC_CREATE_DELETE,
                    )
                )
    return findings


def _node_removal_bound(path: Path) -> frozenset[str] | None:
    """Labels and types `policy.node_removals` lets this file delete, or `None` when unnamed.

    Only names the schema declares count, so a stale entry allows nothing: fail closed."""
    schema = _schema()
    names = schema.policy.removable(path)
    if names is None:
        return None
    declared = set(schema.nodes) | set(schema.relationships)
    return frozenset(name for name in names if name in declared)


def _labels_named_for(code: str, var: str) -> set[str]:
    """Every label this file names for node variable `var`: each label of every
    pattern that binds it (`(n:Movie:Entity ...)`) and each label predicate on it
    (`WHERE n:Person OR n:Movie`). Read over the whole file, as
    `_label_and_type_bindings` reads a binding."""
    names: set[str] = set()
    for m in re.finditer(rf"(?<![\w.$]){re.escape(var)}\s*((?::\s*\w+\s*)+)", code):
        names.update(re.findall(r"\w+", m.group(1)))
    return names


def _deletes_only_deletable_labels(path: Path, code: str, targets: list[str]) -> bool:
    """True when `policy.node_removals` names this file and covers each deleted variable.

    A node qualifies when its labels less the schema's `common_label` are non-empty and all
    in the row; a relationship when its type is.
    """
    bound = _node_removal_bound(path)
    if not targets or not bound:
        return False
    types = _label_and_type_bindings(code)[1]
    common = {_schema().common_label}
    for target in targets:
        if _is_relationship_var(code, target):
            if types.get(target) not in bound:
                return False
            continue
        labels = _labels_named_for(code, target) - common
        if not labels or not labels <= bound:
            return False
    return True


# --- 4. Write paths ----------------------------------------------------------


def _write_path_findings(rel: str, path: Path, code: str) -> list[Finding]:
    """A write clause in a file `policy.write_globs` does not match, and a write file outside
    `policy.write_allowed_globs`; off while `write_globs` is empty."""
    policy = _schema().policy
    if not policy.write_globs or _is_ddl_file(path):
        return []
    if _is_write_file(path):
        if policy.write_allowed_globs and not matches_any(path, policy.write_allowed_globs):
            return [
                Finding(
                    rel,
                    1,
                    1,
                    "cypher-lint-write-outside-capability",
                    "a write query file sits outside the locations allowed to open a write",
                    fix="move it under a location policy.write_allowed_globs names",
                    doc=DOC_WRITE_ONLY_WHERE_ALLOWED,
                )
            ]
        return []
    if not WRITE_CLAUSE_RE.search(code):
        return []
    return [
        Finding(
            rel,
            1,
            1,
            "cypher-lint-write-outside-write-folder",
            "a write clause appears outside a write-query file",
            fix="move this query to a file policy.write_globs matches",
            doc=DOC_WRITE_ONLY_WHERE_ALLOWED,
        )
    ]


# --- 5. Anchor first, expand second -----------------------------------------

# The clause keyword that opens a query part, found on its own so the *first*
# occurrence is fixed before anything asks whether it is anchored -- asking
# "is there SOME anchored MATCH/MERGE" (a plain search over the whole part)
# is exactly the bug this replaces: it can find a later, anchored occurrence
# and never notice the first one was not.
_CLAUSE_START_RE = re.compile(r"\b(?:MATCH|MERGE)\b")
# An anchored first clause: an optional `p =` path assignment, then a node with one or more
# labels (`(m:Movie:Entity {id: $id})`). Matched at the position after a
# `_CLAUSE_START_RE` hit, so it reads only that clause's node.
_ANCHOR_NODE_RE = re.compile(
    r"\s*(?:\w+\s*=\s*)?\(\s*(?P<var>\w*)\s*:\s*(?P<label>\w+)(?:\s*:\s*\w+)*"
    r"\s*(?P<propmap>\{[^}]*\})?\)"
)
# An unlabelled node with an inline map, `MATCH (n {id: $id})`: the anchor rule asks for a
# unique or indexed property, not a label. A common label every node carries is where
# such an `id` constraint typically lives.
_ANCHOR_UNLABELLED_NODE_RE = re.compile(
    r"\s*(?:\w+\s*=\s*)?\(\s*(?P<var>\w*)\s*(?P<propmap>\{[^}]*\})\)"
)
# A node with neither label nor inline map -- `MATCH (n) WHERE n.id IN $ids`.
# Its anchor, if any, is the WHERE that follows (`_where_seeks_anchorable_property`).
_ANCHOR_BARE_NODE_RE = re.compile(r"\s*(?:\w+\s*=\s*)?\(\s*(?P<var>\w+)\s*\)")
# Where one query "part" ends and the next begins: the next top-level clause
# keyword, or a UNION [ALL] joining another query part. WHERE is deliberately
# excluded -- it belongs to the MATCH/MERGE right before it, which is exactly
# the clause an equality-predicate anchor must be read off of.
_NEXT_CLAUSE_RE = re.compile(r"\b(?:MATCH|MERGE|WITH|CALL|RETURN|UNWIND|UNION(?:\s+ALL)?)\b")
_UNION_SPLIT_RE = re.compile(r"\bUNION\b(?:\s+ALL\b)?")


def _uniquely_constrained_properties() -> frozenset[str]:
    """Uniquely constrained property names, from the schema's `constraints.unique`."""
    return _schema().unique_properties


def _anchorable_properties() -> frozenset[str]:
    """Every property a read may anchor on: unique constraints and range indexes alike."""
    return _schema().anchorable_properties


def _seeks_anchorable_property(propmap: str) -> bool:
    """True when an inline property map keys on an anchorable property, by
    `$parameter` -- a literal is not an anchor a caller supplies."""
    anchorable = _anchorable_properties()
    return any(
        match.group(1) in anchorable and re.match(r"\s*\$", propmap[match.end() :]) is not None
        for match in _top_level_prop_keys(propmap, 0, len(propmap))
    )


def _where_seeks_anchorable_property(code: str, start: int, end: int, var: str) -> bool:
    """True when the WHERE right after a node seeks an anchorable property of
    `var` by `=` or `IN` -- the same index seek the labelled branch accepts,
    for a node written without a label."""
    if not var:
        return False
    boundary = _NEXT_CLAUSE_RE.search(code, start, end)
    block = code[start : boundary.start() if boundary is not None else end]
    anchorable = _anchorable_properties()
    return any(
        match.group(1) in anchorable
        for match in re.finditer(
            rf"\bWHERE\b.*?\b{re.escape(var)}\.(\w+)\s*(?:=|IN\s*[$\[])", block, re.DOTALL
        )
    )


def _clause_seeks_anchorable_property(code: str, start: int, end: int) -> bool:
    """True when any node in the first clause's pattern keys on an anchorable property.

    `(p)-[:ACTED_IN]->(m {id: $id})` seeks through `m` and Neo4j plans the seek first, so
    where the seek is written does not decide.
    """
    boundary = _NEXT_CLAUSE_RE.search(code, start, end)
    clause_end = boundary.start() if boundary is not None else end
    return any(
        _seeks_anchorable_property(match.group(0))
        for match in re.finditer(r"\{[^{}]*\}", code[start:clause_end])
    )


def _line_of(code: str, pos: int) -> int:
    return code.count("\n", 0, pos) + 1


def _query_parts(code: str) -> list[tuple[int, int]]:
    """`code` split into `(start, end)` offsets on top-level `UNION`/`UNION
    ALL` boundaries -- each part is independently anchored, so
    a later part's anchored MATCH must never hide an earlier part's
    unanchored one, and vice versa."""
    unions = list(_UNION_SPLIT_RE.finditer(code))
    starts = [0] + [m.end() for m in unions]
    ends = [m.start() for m in unions] + [len(code)]
    return list(zip(starts, ends, strict=True))


def _unlabelled_clause_is_anchored(code: str, clause_end: int, end: int) -> bool:
    """A first `MATCH`/`MERGE` whose node carries no label is anchored only by a
    seek on an anchorable property -- written inline (`MATCH (n {id: $id})`) or
    in the `WHERE` right after it (`MATCH (n) WHERE n.id IN $ids`). Both are
    index seeks, not label scans, which is what the anchor rule
    actually asks for."""
    unlabelled = _ANCHOR_UNLABELLED_NODE_RE.match(code, clause_end, end)
    if unlabelled is not None and _seeks_anchorable_property(unlabelled.group("propmap")):
        return True
    bare = _ANCHOR_BARE_NODE_RE.match(code, clause_end, end)
    return bare is not None and _where_seeks_anchorable_property(
        code, bare.end(), end, bare.group("var")
    )


def _labelled_clause_is_anchored(code: str, clause_end: int, end: int, node: re.Match[str]) -> bool:
    """A first `MATCH`/`MERGE` whose node carries a label is anchored by its own
    inline key, by an anchorable-property seek on a *later* node of the same pattern,
    or by an equality/`IN` predicate on its variable in the following `WHERE`."""
    propmap = node.group("propmap")
    if propmap and _PROP_KEY_RE.search(propmap):
        return True
    # A later node in the same pattern may carry the seek -- Neo4j plans it
    # first whichever end of the pattern it is written from.
    if _clause_seeks_anchorable_property(code, clause_end, end):
        return True
    var = node.group("var")
    if not var:
        return False
    boundary = _NEXT_CLAUSE_RE.search(code, node.end(), end)
    block = code[node.end() : boundary.start() if boundary is not None else end]
    # Equality or `IN` against a parameter or list literal is an index seek on a unique or
    # indexed property; `>`, `CONTAINS`,
    # `STARTS WITH` and the like leave the part unanchored.
    return (
        re.search(rf"\bWHERE\b.*?\b{re.escape(var)}\.\w+\s*(?:=|IN\s*[$\[])", block, re.DOTALL)
        is not None
    )


def _part_is_anchored(code: str, start: int, end: int) -> tuple[bool, int]:
    """`(is_anchored, position_of_the_first_MATCH/MERGE_or_of_the_part_start)`
    for the query part `code[start:end]`. A part with no MATCH/MERGE at all
    (pure `WITH`/`RETURN`/`CALL`) has nothing to anchor and counts as
    anchored; otherwise the labelled and unlabelled branches above decide,
    each on the single question "does this clause seek, or does it scan?"."""
    clause = _CLAUSE_START_RE.search(code, start, end)
    if clause is None:
        return True, start
    node = _ANCHOR_NODE_RE.match(code, clause.end(), end)
    anchored = (
        _labelled_clause_is_anchored(code, clause.end(), end, node)
        if node is not None
        else _unlabelled_clause_is_anchored(code, clause.end(), end)
    )
    return anchored, clause.start()


def _anchor_findings(rel: str, path: Path, code: str) -> list[Finding]:
    if _is_ddl_file(path):
        return []
    if _is_scan_shaped(path):
        return []
    findings = []
    for start, end in _query_parts(code):
        anchored, pos = _part_is_anchored(code, start, end)
        if not anchored:
            findings.append(
                Finding(
                    rel,
                    _line_of(code, pos),
                    1,
                    "cypher-lint-not-anchored",
                    "the first MATCH/MERGE does not anchor on a label plus either an "
                    "inline property map or an equality predicate on that property in "
                    "the following WHERE",
                    fix="anchor on a label and an indexed/unique property, "
                    "or register this query under policy.scan_shaped_queries",
                    doc=DOC_ANCHOR,
                )
            )
    return findings


# --- 6. Relationship type vocabulary -----------------------------------------


def _named_relationship_types(code: str) -> list[tuple[int, int, str]]:
    """`(line, col, type)` for every relationship type a pattern names, one
    entry per name in an alternation. `line`/`col` are the name's own, so a
    finding on `[:ACTED_IN|NOT_A_TYPE]` points at the second name."""
    found: list[tuple[int, int, str]] = []
    for match in _REL_TYPE_EXPR_RE.finditer(code):
        expr_start = match.start("types")
        offset = 0
        for part in match.group("types").split("|"):
            # `part` may carry leading whitespace and the repeated `:` of the
            # `[:A|:B]` spelling; the name starts where those end.
            name = part.strip().lstrip(":").strip()
            if name:
                pos = expr_start + offset + part.index(name)
                found.append((_line_of(code, pos), pos - code.rfind("\n", 0, pos), name))
            offset += len(part) + 1
    return found


def _relationship_type_findings(rel: str, code: str) -> list[Finding]:
    """Every relationship type named in a pattern is one the schema declares.

    An unknown type is a typo or a retired name that changes an edge's meaning; the live
    validator catches it only against a database, this static twin runs without one."""
    vocabulary = set(_schema().relationships)
    if not vocabulary:
        return []
    return [
        Finding(
            rel,
            line,
            col,
            "cypher-lint-unknown-relationship-type",
            f"{name!r} is not a declared relationship type",
            fix="use a declared relationship type, or declare the new one in the schema first",
            doc=DOC_SCHEMA_VOCABULARY,
        )
        for line, col, name in _named_relationship_types(code)
        if name not in vocabulary
    ]


# --- 7. No templating / dynamic labels ---------------------------------------


def _no_templating_findings(rel: str, code: str) -> list[Finding]:
    return [
        Finding(
            rel,
            lineno,
            1,
            "cypher-lint-templating-marker",
            f"templating/dynamic-label marker found: {pattern.pattern}",
            fix="parameterize the value; never build a label or clause dynamically",
            doc=DOC_PARAMETERIZE,
        )
        for lineno, line in enumerate(code.splitlines(), start=1)
        for pattern in _TEMPLATING_RES
        if pattern.search(line)
    ]


# --- 8. Formatting ------------------------------------------------------------


def _previous_non_blank(raw_lines: list[str], idx: int) -> tuple[int, str]:
    while idx >= 0 and not raw_lines[idx].strip():
        idx -= 1
    return idx, ("" if idx < 0 else raw_lines[idx].strip())


def _opens_a_clause(stripped: str) -> bool:
    return any(
        re.match(rf"{re.escape(kw)}(?!\w)", stripped, re.IGNORECASE) for kw in CLAUSE_KEYWORDS
    )


def _quantified_pattern_is_commented(raw_lines: list[str], lineno: int) -> bool:
    """Whether a comment sits above the quantified pattern at `lineno`.

    The pattern is walked back to its opening clause keyword; a comment above that line or
    inside the pattern above the quantifier counts. The walk never crosses the keyword, and
    `--` is a path arrow, never a comment."""
    idx = lineno - 1
    while idx >= 0 and not _opens_a_clause(raw_lines[idx].strip()):
        if raw_lines[idx].strip().startswith(("//", "/*")):
            return True  # a comment written inside the pattern, above the quantifier
        idx -= 1
    if idx < 0:
        return False
    _, above_clause = _previous_non_blank(raw_lines, idx - 1)
    return above_clause.startswith(("//", "/*"))


def _formatting_findings(rel: str, code: str, raw: str | None = None) -> list[Finding]:
    """Clause keyword, one-MATCH-per-line and quantified-pattern comment rules.

    Only `//` and `/* */` are comments; `--` is a path arrow (`-->(m:Movie)`)."""
    findings = []
    lines = code.splitlines()
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        for keyword in CLAUSE_KEYWORDS:
            lowered_kw = keyword.lower()
            # Word-boundary, not a bare prefix test: a
            # continuation line like `created_at = $now` must never match
            # `CREATE` just because "created" starts with "create".
            if re.match(
                rf"{re.escape(lowered_kw)}(?!\w)", stripped.lower()
            ) and not stripped.startswith(keyword):
                findings.append(
                    Finding(
                        rel,
                        lineno,
                        1,
                        "cypher-lint-keyword-not-uppercase",
                        f"clause keyword should be uppercase: {keyword}",
                        fix=f"write {keyword} in upper case",
                        doc=DOC_FORMATTING,
                    )
                )
                break
        if len(re.findall(r"\bMATCH\b", stripped)) > 1:
            findings.append(
                Finding(
                    rel,
                    lineno,
                    1,
                    "cypher-lint-multiple-match-per-line",
                    "more than one MATCH/OPTIONAL MATCH on this line",
                    fix="one MATCH per line",
                    doc=DOC_FORMATTING,
                )
            )
        if re.search(r"\)\s*\{\s*\d*\s*,\s*\d+\s*\}", stripped):
            raw_lines = raw.splitlines() if raw is not None else lines
            if not _quantified_pattern_is_commented(raw_lines, lineno):
                findings.append(
                    Finding(
                        rel,
                        lineno,
                        1,
                        "cypher-lint-qpp-uncommented",
                        "a quantified path pattern has no comment above it",
                        fix="add a comment above explaining the quantified pattern",
                        doc=DOC_FORMATTING,
                    )
                )
    return findings


# --- 9. SET/CREATE/MERGE property keys are declared -------------------------

# A node pattern with an explicit label, the var/label binding this rule reads. Its `{...}`
# map is found by the balanced `_map_span_at`/`_top_level_prop_keys` scan so a nested
# constructor map (`datetime({...})`) never lends its keys. An unlabelled `(f)` is skipped.
_NODE_LABEL_RE = re.compile(r"\(\s*(?P<var>\w+)?\s*:\s*(?P<label>\w+)(?:\s*:\s*\w+)*\s*")
# A relationship pattern with an explicit type (`[r:ACTED_IN {...}]`,
# `[:DIRECTED]`) -- same shape, one level down. `rest` still stops at this
# pattern's own `]`, so it can never run into a later pattern's map.
_REL_TYPE_RE = re.compile(r"\[\s*(?P<var>\w+)?\s*:\s*(?P<type>\w+)(?P<rest>[^\]]*)\]")
# `var.prop = ...` / `var.prop += ...`, never `var.prop == ...` (a WHERE
# comparison; excluded by the run of clause keywords `_SET_CLAUSE_END_RE`
# already bounds this search to, so an `==` inside a SET body should not
# occur, but the negative lookahead keeps the pattern honest on its own).
_SET_DOT_ASSIGN_RE = re.compile(r"\b(\w+)\.(\w+)\s*\+?=(?!=)")
# `var += {...}` / `var = {...}` -- a whole-map merge or overwrite. A
# zero-width lookahead on the opening `{`; the map itself is found by the
# balanced `_map_span_at` scan.
_SET_MAP_ASSIGN_RE = re.compile(r"\b(\w+)\s*\+?=(?!=)\s*(?=\{)")
_SET_CLAUSE_START_RE = re.compile(r"\bSET\b")
_SET_CLAUSE_END_RE = re.compile(
    r"\b(?:MATCH|MERGE|WITH|CALL|RETURN|UNWIND|WHERE|CREATE|DELETE|REMOVE|FOREACH|SET"
    r"|UNION(?:\s+ALL)?)\b"
)


def _label_and_type_bindings(code: str) -> tuple[dict[str, str], dict[str, str]]:
    """`(var -> label, var -> relationship type)`, gathered once over the
    whole file: a variable is bound where its pattern first names a label
    or type, and every later `SET`/map reference to that same variable (the
    common shape -- bind in `MATCH`/`MERGE`, mutate in `SET`) resolves
    against it."""
    labels: dict[str, str] = {}
    for m in _NODE_LABEL_RE.finditer(code):
        var = m.group("var")
        if var:
            labels.setdefault(var, m.group("label"))
    types: dict[str, str] = {}
    for m in _REL_TYPE_RE.finditer(code):
        var = m.group("var")
        if var:
            types.setdefault(var, m.group("type"))
    return labels, types


def _documented_node_properties(schema: GraphSchema, label: str) -> set[str] | None:
    return schema.node_property_names(label)


def _documented_rel_properties(schema: GraphSchema, rel_type: str) -> set[str] | None:
    return schema.relationship_property_names(rel_type)


def _unknown_property_finding(
    rel: str, code: str, pos: int, subject: str, prop: str, *, doc: str
) -> Finding:
    return Finding(
        rel,
        _line_of(code, pos),
        pos - code.rfind("\n", 0, pos),
        "cypher-lint-set-unknown-property",
        f"{prop!r} is not a declared property of {subject}",
        fix="use a declared property name, or declare the new one in the schema first",
        doc=doc,
    )


def _node_map_property_findings(rel: str, code: str, schema: GraphSchema) -> list[Finding]:
    """Inline node maps: `(p:Person {bad_key: 1})`, in a `CREATE`/`MERGE`
    pattern (or a `MATCH` filter, checked the same way -- a declared
    property is a declared property regardless of clause)."""
    findings: list[Finding] = []
    for m in _NODE_LABEL_RE.finditer(code):
        span = _map_span_at(code, _skip_space(code, m.end()))
        if span is None:
            continue
        allowed = _documented_node_properties(schema, m.group("label"))
        if allowed is None:
            continue
        subject = f"the {m.group('label')!r} node"
        findings.extend(
            _unknown_property_finding(
                rel, code, km.start(), subject, km.group(1), doc=DOC_SCHEMA_VOCABULARY
            )
            for km in _top_level_prop_keys(code, *span)
            if km.group(1) not in allowed
        )
    return findings


def _rel_map_property_findings(rel: str, code: str, schema: GraphSchema) -> list[Finding]:
    """Inline relationship maps: `[r:ACTED_IN {bad_key: 1}]`."""
    findings: list[Finding] = []
    for m in _REL_TYPE_RE.finditer(code):
        rest = m.group("rest")
        brace = rest.find("{")
        if brace == -1:
            continue
        span = _map_span_at(code, m.start("rest") + brace)
        if span is None:
            continue
        allowed = _documented_rel_properties(schema, m.group("type"))
        if allowed is None:
            continue
        subject = f"the {m.group('type')!r} relationship"
        findings.extend(
            _unknown_property_finding(
                rel, code, km.start(), subject, km.group(1), doc=DOC_SCHEMA_VOCABULARY
            )
            for km in _top_level_prop_keys(code, *span)
            if km.group(1) not in allowed
        )
    return findings


def _bound_subject(
    schema: GraphSchema, labels: dict[str, str], types: dict[str, str], var: str
) -> tuple[set[str], str, str] | None:
    """`(allowed_property_names, subject_text, doc_anchor)` for `var`, or `None`.

    `None` when `var` is unbound or its label/type undeclared: nothing to compare against."""
    if var in labels:
        allowed = _documented_node_properties(schema, labels[var])
        if allowed is None:
            return None
        return allowed, f"the {labels[var]!r} node", DOC_SCHEMA_VOCABULARY
    if var in types:
        allowed = _documented_rel_properties(schema, types[var])
        if allowed is None:
            return None
        return allowed, f"the {types[var]!r} relationship", DOC_SCHEMA_VOCABULARY
    return None


def _set_clause_property_findings(
    rel: str,
    code: str,
    schema: GraphSchema,
    labels: dict[str, str],
    types: dict[str, str],
) -> list[Finding]:
    """`SET var.prop = ...` / `+=`, and `SET var += {...}` map merges.

    Neither the live validator nor the plan check catches this shape; maps are read by the balanced
    `_map_span_at`/`_top_level_prop_keys` scan."""
    findings: list[Finding] = []
    for set_match in _SET_CLAUSE_START_RE.finditer(code):
        end_match = _SET_CLAUSE_END_RE.search(code, set_match.end())
        end = end_match.start() if end_match else len(code)
        body = code[set_match.end() : end]
        offset = set_match.end()
        for dm in _SET_DOT_ASSIGN_RE.finditer(body):
            bound = _bound_subject(schema, labels, types, dm.group(1))
            if bound is None:
                continue
            allowed, subject, doc = bound
            if dm.group(2) not in allowed:
                pos = offset + dm.start()
                findings.append(
                    _unknown_property_finding(rel, code, pos, subject, dm.group(2), doc=doc)
                )
        for mm in _SET_MAP_ASSIGN_RE.finditer(body):
            bound = _bound_subject(schema, labels, types, mm.group(1))
            if bound is None:
                continue
            allowed, subject, doc = bound
            span = _map_span_at(code, offset + mm.end())
            if span is None:
                continue
            findings.extend(
                _unknown_property_finding(rel, code, km.start(), subject, km.group(1), doc=doc)
                for km in _top_level_prop_keys(code, *span)
                if km.group(1) not in allowed
            )
    return findings


def _set_property_findings(rel: str, code: str) -> list[Finding]:
    schema = _schema()
    labels, types = _label_and_type_bindings(code)
    return [
        *_node_map_property_findings(rel, code, schema),
        *_rel_map_property_findings(rel, code, schema),
        *_set_clause_property_findings(rel, code, schema, labels, types),
    ]


# --- 10. Set shared properties once ----------------------------------------

_ON_SET_RE = re.compile(r"\bMERGE\b|\bON\s+(?P<branch>CREATE|MATCH)\s+SET\b")
_ON_SET_END_RE = re.compile(r"\bON\s+(?:CREATE|MATCH)\b|" + _SET_CLAUSE_END_RE.pattern)


def _top_level_assignments(code: str, raw: str, start: int, end: int) -> list[tuple[int, str]]:
    """The comma-separated assignments of `code[start:end]` as `(position,
    whitespace-normalised text)`. Commas are split on `code` (string
    interiors blanked, so a comma inside a value never splits) and the text
    is read from `raw` (same offsets, strings intact, so `'A'` and `'B'`
    differ)."""
    out: list[tuple[int, str]] = []
    depth, piece = 0, start
    for i in range(start, end + 1):
        ch = code[i] if i < end else ","
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            text = " ".join(raw[piece:i].split())
            if text:
                out.append((piece + len(raw[piece:i]) - len(raw[piece:i].lstrip()), text))
            piece = i + 1
    return out


def _shared_set_findings(rel: str, code: str, raw: str) -> list[Finding]:
    """Per `MERGE`, one finding naming the assignments the second `ON ... SET`
    branch repeats verbatim from the first. `raw` is the comment-blanked text with string interiors
    intact, offset-for-offset equal to `code`."""
    findings: list[Finding] = []
    seen: dict[str, dict[str, int]] = {}
    for m in _ON_SET_RE.finditer(code):
        branch = m.group("branch")
        if branch is None:
            seen = {}
            continue
        end_match = _ON_SET_END_RE.search(code, m.end())
        end = end_match.start() if end_match else len(code)
        assignments = _top_level_assignments(code, raw, m.end(), end)
        other = seen.get("MATCH" if branch == "CREATE" else "CREATE", {})
        shared = [(pos, text) for pos, text in assignments if text in other]
        if shared:
            pos = shared[0][0]
            targets = ", ".join(text.split("=", 1)[0].strip() for _, text in shared)
            findings.append(
                Finding(
                    rel,
                    _line_of(code, pos),
                    pos - code.rfind("\n", 0, pos),
                    "cypher-lint-shared-set-in-both-branches",
                    f"{targets} written identically in both ON CREATE SET and ON MATCH SET",
                    fix=(
                        "move them to a plain SET after the MERGE; "
                        "keep only the differences in the branches"
                    ),
                    doc=DOC_SHARED_SET,
                )
            )
        seen[branch] = {text: pos for pos, text in assignments}
    return findings


# --- 11. Vectors stay in the graph -------------------------------------------

#: Every clause keyword a projection's extent can end at. Matched case-sensitively
#: over `code` (comments and string interiors blanked), where the formatting
#: rule already holds every keyword uppercase, so `$limit` or `node.order` is
#: never read as one. `WITH` here is the clause, never `STARTS WITH`/`ENDS WITH`
#: (`_clause_tokens` drops those).
_CLAUSE_TOKEN_RE = re.compile(
    r"\b(?P<kw>OPTIONAL\s+MATCH|ORDER\s+BY|DETACH\s+DELETE|UNION(?:\s+ALL)?|MATCH|MERGE|"
    r"CREATE|SET|DELETE|REMOVE|FOREACH|WHERE|WITH|RETURN|UNWIND|CALL|YIELD|SKIP|LIMIT)\b"
)
_STRING_PREDICATE_TAIL_RE = re.compile(r"\b(?:STARTS|ENDS)\s*$")
_PROJECTION_KWS = frozenset({"WITH", "RETURN"})
#: A call whose argument a vector may be, because its result is not the vector:
#: a length, a similarity score, or a fold over the values.
_VECTOR_REDUCER_RE = re.compile(r"(?:\bsize|\bvector\.similarity\.\w+|\breduce)\s*\($")
_NULL_TEST_RE = re.compile(r"\s*IS\s+(?:NOT\s+)?NULL\b")
_DIRECTION_RE = re.compile(r"\s+(?:ASC|ASCENDING|DESC|DESCENDING)\s*$")
_ALIAS_RE = re.compile(r"^(?P<expr>.*?)\s+AS\s+(?P<alias>\w+)$", re.DOTALL)
_BARE_NAME_RE = re.compile(r"\w+")
#: A node pattern that binds a variable: `(n)`, `(n:Movie)`, `(n:Movie:Entity {...})`,
#: `(x:Movie|Person WHERE ...)` -- never a call's argument, `count(n)`, which a word precedes.
_NODE_PATTERN_RE = re.compile(
    r"(?<![\w.$])\(\s*(?P<var>\w+)\s*(?P<labels>(?::\s*!?\w+(?:\s*[:|&]\s*!?\w+)*)?)"
    r"\s*(?=[){]|WHERE\b)"
)
#: A path assignment, `p = (...)` or `p = shortestPath(...)`, read inside a `MATCH`/`MERGE`.
_PATH_ASSIGN_RE = re.compile(r"(?<![\w.$<>!=+])(?P<var>\w+)\s*=(?!=)")
_PATTERN_CLAUSES = frozenset({"MATCH", "OPTIONAL MATCH", "MERGE"})
#: A list comprehension's or a list predicate's element, `[c IN cmps | ...]`,
#: `any(c IN cmps WHERE ...)`: it holds what an element of its list holds.
_ELEMENT_BINDING_RE = re.compile(
    r"(?:\[|\b(?:any|all|none|single)\s*\()\s*(?P<var>\w+)\s+IN\s+"
    r"(?P<expr>\$?[\w.]+(?:\([^()]*\))?)"
)
#: A call whose result holds the nodes of its argument: a list of them, one of them, or a
#: path's nodes.
_NODE_HOLDER_RE = re.compile(
    r"^(?:collect|head|last|tail|reverse|nodes)\s*\(\s*(?:DISTINCT\s+)?(?P<arg>.*)\)$", re.DOTALL
)
#: The expression an `AS` names, read back from just before the `AS`: a property, a call
#: on plain arguments, or a variable -- never a parameter.
_ALIASED_EXPRESSION_RE = re.compile(r"(?<![\w.$])(?P<expr>[\w.]+(?:\([^()]*\))?)\s*$")
_MAP_PROJECTION_RE = re.compile(r"\b(?P<var>\w+)\s*(?=\{)")
#: A property read, `n.prop`, or a map-projection shorthand, `{.prop}`.
_PROPERTY_ACCESS_RE = re.compile(r"\.\s*(\w+)")


def _depths(code: str) -> list[int]:
    """The bracket depth -- `(`, `[` and `{` alike -- *before* each character of
    `code`, so a clause keyword at depth 0 is a statement-level clause and one at
    depth `d` belongs to the subquery or expression that opened it."""
    depths: list[int] = []
    depth = 0
    for ch in code:
        if ch in "([{":
            depths.append(depth)
            depth += 1
        elif ch in ")]}":
            depth -= 1
            depths.append(depth)
        else:
            depths.append(depth)
    return depths


@dataclass(frozen=True)
class _ClauseToken:
    kw: str
    start: int
    end: int
    depth: int


def _clause_tokens(code: str, depths: list[int]) -> list[_ClauseToken]:
    tokens: list[_ClauseToken] = []
    for match in _CLAUSE_TOKEN_RE.finditer(code):
        kw = " ".join(match.group("kw").split())
        if kw == "WITH" and _STRING_PREDICATE_TAIL_RE.search(code, 0, match.start()):
            continue
        tokens.append(_ClauseToken(kw, match.start(), match.end(), depths[match.start()]))
    return tokens


def _clause_end(tokens: list[_ClauseToken], after: int, depth: int, default: int) -> int:
    """Where the clause opened before `after` ends: the next keyword at the same depth,
    or the close of the bracket that holds it (`default` when nothing follows)."""
    return next((t.start for t in tokens if t.start >= after and t.depth == depth), default)


def _split_top_level(code: str, depths: list[int], start: int, end: int) -> list[tuple[int, str]]:
    """`code[start:end]` split at the commas of its own depth, each piece with its
    offset and stripped of surrounding space."""
    base = depths[start] if start < len(depths) else 0
    pieces: list[tuple[int, str]] = []
    piece_start = start
    for i in range(start, end):
        if code[i] == "," and depths[i] == base:
            pieces.append((piece_start, code[piece_start:i]))
            piece_start = i + 1
    pieces.append((piece_start, code[piece_start:end]))
    return [
        (offset + len(text) - len(text.lstrip()), " ".join(text.split()))
        for offset, text in pieces
        if text.strip()
    ]


def _vector_properties_by_label() -> dict[str, frozenset[str]]:
    """Each node label's vector-typed properties, its own and the common ones -- read from
    the schema, never a list of names kept here."""
    return _schema().vector_properties_by_label()


def _vector_property_names() -> frozenset[str]:
    return frozenset(name for names in _vector_properties_by_label().values() for name in names)


def _label_expression_vectors(labels: str) -> frozenset[str]:
    """The vectors a node of a label expression (`:A`, `:A:B`, `:A|B`, `:A&B`) may
    carry. Each alternative is pinned by its known labels; an alternative naming
    none -- the common label, a negation, a label the schema does not type --
    may be any node, so it carries every vector."""
    by_label = _vector_properties_by_label()
    every = _vector_property_names()
    carried: set[str] = set()
    for alternative in labels.lstrip(" :").split("|"):
        names = [n.strip() for n in re.split(r"[:&]", alternative) if n.strip()]
        known = [n for n in names if n in by_label]
        carried |= every if not known else {v for n in known for v in by_label[n]}
    return frozenset(carried)


@dataclass(frozen=True)
class _Binding:
    """From `pos` on, `var` holds a node, a list or a path whose nodes may carry
    `carried` -- or, where `carried` is `None`, a value that is no node."""

    pos: int
    var: str
    carried: frozenset[str] | None


def _bindings_before(bindings: list[_Binding], pos: int) -> dict[str, frozenset[str] | None]:
    state: dict[str, frozenset[str] | None] = {}
    for binding in bindings:
        if binding.pos >= pos:
            break
        state[binding.var] = binding.carried
    return state


def _expression_carries(
    expr: str, state: dict[str, frozenset[str] | None]
) -> frozenset[str] | None:
    """What a projected expression holds: a bare variable what it is bound to, a
    `collect`/`head`/`nodes`/... of one the nodes of its argument; any other
    expression a value."""
    expr = expr.strip().removeprefix("DISTINCT ").strip()
    if _BARE_NAME_RE.fullmatch(expr):
        return state.get(expr)
    holder = _NODE_HOLDER_RE.match(expr)
    return _expression_carries(holder.group("arg"), state) if holder else None


def _clause_extent(tokens: list[_ClauseToken], depths: list[int], token: _ClauseToken) -> int:
    """Where `token`'s clause ends: the next keyword of its depth or the close of
    the bracket that holds it, whichever comes first."""
    close = next((i for i in range(token.end, len(depths)) if depths[i] < token.depth), len(depths))
    return min(_clause_end(tokens, token.end, token.depth, close), close)


_BINDING_KWS = frozenset({"WITH", "RETURN", "UNWIND", "YIELD"})
#: `(pos, kind, var, detail)`: a node `pattern` binding `var` to the label expression
#: `detail`; `every`, a node of any label (a path, a `YIELD node`); `value`, no node; an
#: `alias` binding `var` to what the expression `detail` holds.
_BindingEvent = tuple[int, str, str, str]


def _in_pattern_clause(tokens: list[_ClauseToken], depths: list[int], pos: int) -> bool:
    clause = next((t for t in reversed(tokens) if t.start < pos and t.depth <= depths[pos]), None)
    return clause is not None and clause.kw in _PATTERN_CLAUSES


def _projection_events(
    code: str, depths: list[int], tokens: list[_ClauseToken], token: _ClauseToken
) -> list[_BindingEvent]:
    """The names a `WITH`/`RETURN`/`UNWIND`/`YIELD` binds, each bound after its
    clause, as Cypher scopes them. An index procedure's `node` column is a node of
    any label the index covers."""
    end = _clause_extent(tokens, depths, token)
    events: list[_BindingEvent] = []
    for _offset, raw in _split_top_level(code, depths, token.end, end):
        item = raw.removeprefix("DISTINCT ")
        alias = _ALIAS_RE.match(item)
        if token.kw == "YIELD":
            column = alias.group("expr") if alias else item
            var = alias.group("alias") if alias else item
            events.append((end, "every" if column == "node" else "value", var, ""))
        elif alias:
            events.append((end, "alias", alias.group("alias"), alias.group("expr")))
    return events


def _binding_events(code: str, depths: list[int]) -> list[_BindingEvent]:
    tokens = _clause_tokens(code, depths)
    events: list[_BindingEvent] = [
        (m.start(), "pattern", m.group("var"), m.group("labels"))
        for m in _NODE_PATTERN_RE.finditer(code)
    ]
    events += [
        (m.start(), "every", m.group("var"), "")
        for m in _PATH_ASSIGN_RE.finditer(code)
        if _in_pattern_clause(tokens, depths, m.start())
    ]
    events += [
        (m.start("var"), "alias", m.group("var"), m.group("expr"))
        for m in _ELEMENT_BINDING_RE.finditer(code)
    ]
    for token in tokens:
        if token.kw in _BINDING_KWS:
            events += _projection_events(code, depths, tokens, token)
    return sorted(events, key=lambda event: event[0])


def _event_carries(
    event: _BindingEvent, state: dict[str, frozenset[str] | None]
) -> frozenset[str] | None:
    _pos, kind, var, detail = event
    if kind == "pattern" and detail:
        return _label_expression_vectors(detail) | (state.get(var) or frozenset())
    if kind == "pattern":
        # A pattern naming a bound variable again keeps its binding.
        return state[var] if var in state else _vector_property_names()
    if kind == "every":
        return _vector_property_names()
    if kind == "value":
        return None
    return _expression_carries(detail, state)


def _node_bindings(code: str, depths: list[int]) -> list[_Binding]:
    """Every variable binding of the file, in order, with the vectors it may carry.

    Nodes carry their labels' vectors (all when unlabelled), paths and `YIELD node` all;
    `AS`/`IN` re-bindings carry what their expression holds (`_expression_carries`).
    A node it cannot pin carries every vector the schema types: fail closed."""
    bindings: list[_Binding] = []
    state: dict[str, frozenset[str] | None] = {}
    for event in _binding_events(code, depths):
        carried = _event_carries(event, state)
        state[event[2]] = carried
        bindings.append(_Binding(event[0], event[2], carried))
    return bindings


def _is_registered_vector_read(path: Path) -> bool:
    """Whether `policy.vector_read_queries` names this file. While the register is empty the
    allowance is empty, which fails closed."""
    return _schema().policy.is_vector_read(path)


def _enclosing_call(code: str, depths: list[int], pos: int, floor: int) -> str:
    """The text before each bracket enclosing `pos`, innermost first, down to the
    span's own depth `floor` -- joined so a reducer at any level is visible."""
    heads: list[str] = []
    depth = depths[pos]
    i = pos - 1
    while depth > floor and i >= 0:
        if code[i] in "([{" and depths[i] == depth - 1:
            heads.append(code[max(0, i - 40) : i + 1])
            depth -= 1
        i -= 1
    return "\n".join(heads)


def _vector_access_is_reduced(
    code: str, depths: list[int], match: re.Match[str], floor: int
) -> bool:
    if _NULL_TEST_RE.match(code, match.end()):
        return True
    return any(
        _VECTOR_REDUCER_RE.search(head)
        for head in _enclosing_call(code, depths, match.start(), floor).split("\n")
    )


def _top_level_return_spans(code: str, depths: list[int]) -> list[tuple[int, int]]:
    """Every statement-level `RETURN`'s projection, one per `UNION` part -- a
    `CALL { ... RETURN }` subquery's rows are its outer query's to project."""
    tokens = _clause_tokens(code, depths)
    return [
        (t.end, _clause_end(tokens, t.end, 0, len(code)))
        for t in tokens
        if t.kw == "RETURN" and t.depth == 0
    ]


def _vector_finding(rel: str, code: str, pos: int, what: str) -> Finding:
    return Finding(
        rel,
        _line_of(code, pos),
        1,
        "cypher-lint-unregistered-vector-read",
        f"{what}, but policy.vector_read_queries does not name this file",
        fix="return the similarity the graph computes (vector.similarity.cosine or a "
        "vector-index query), null the vector properties of a `.*` projection, or "
        "register the file under policy.vector_read_queries",
        doc=DOC_RETURN_ONLY,
    )


def _vector_projection_findings(
    rel: str, code: str, depths: list[int], span: tuple[int, int], bindings: list[_Binding]
) -> list[Finding]:
    start, end = span
    floor = depths[start] if start < len(depths) else 0
    names = _vector_property_names()
    findings = [
        _vector_finding(rel, code, m.start(), f"the projection returns the vector '{m.group(1)}'")
        for m in _PROPERTY_ACCESS_RE.finditer(code, start, end)
        if m.group(1) in names and not _vector_access_is_reduced(code, depths, m, floor)
    ]
    for m in _MAP_PROJECTION_RE.finditer(code, start, end):
        brace = _skip_space(code, m.end())
        map_span = _map_span_at(code, brace)
        if map_span is None or ".*" not in code[map_span[0] : map_span[1]]:
            continue
        nulled = {k.group(1) for k in _top_level_prop_keys(code, *map_span)}
        carried = _bindings_before(bindings, m.start()).get(m.group("var")) or frozenset()
        missing = sorted(carried - nulled)
        if missing:
            findings.append(
                _vector_finding(
                    rel, code, m.start(), f"'{m.group('var')} {{.*}}' returns the vectors {missing}"
                )
            )
    for offset, raw in _split_top_level(code, depths, start, end):
        item = raw.removeprefix("DISTINCT ")
        alias = _ALIAS_RE.match(item)
        expr = alias.group("expr") if alias else item
        vectors = _expression_carries(expr, _bindings_before(bindings, offset))
        if vectors:
            findings.append(
                _vector_finding(
                    rel,
                    code,
                    offset,
                    f"the whole node '{expr}' returns the vectors {sorted(vectors)}",
                )
            )
    return findings


def _vector_return_findings(rel: str, path: Path, code: str) -> list[Finding]:
    """A `RETURN` projecting a vector in a file the vector-read register does not name.

    Access, a `.*` projection not nulling every vector, or a whole node, node list or path
    counts, per `_node_bindings`. A reduced access (`size`, `vector.similarity.*`, `reduce`,
    `IS [NOT] NULL`) returns no vector."""
    if _is_ddl_file(path) or _is_registered_vector_read(path):
        return []
    depths = _depths(code)
    bindings = _node_bindings(code, depths)
    return [
        finding
        for span in _top_level_return_spans(code, depths)
        for finding in _vector_projection_findings(rel, code, depths, span, bindings)
    ]


# --- 12. Every cut is deterministic ------------------------------------------


def _aliased_expression(code: str, before: int, name: str) -> tuple[str, int] | None:
    """The expression the last `... AS name` before `before` names, with its
    offset -- `None` when no alias binds `name` or its expression is not one
    `_ALIASED_EXPRESSION_RE` reads (a parameter, a nested call)."""
    aliases = list(re.finditer(rf"\bAS\s+{re.escape(name)}\b", code[:before]))
    if not aliases:
        return None
    expr = _ALIASED_EXPRESSION_RE.search(code, 0, aliases[-1].start())
    return (expr.group("expr"), expr.start("expr")) if expr else None


def _uniquely_keyed(key: str, code: str, before: int, bindings: list[_Binding]) -> bool:
    """Whether an `ORDER BY` key is a unique key: a uniquely constrained property
    (the schema's `constraints.unique`) read off a node variable,
    `elementId(...)`, or an alias of one of those projected earlier. A bare name
    is a variable, never a property, so it is unique only as such an alias; a
    property of a value -- a parameter row, a map -- is no node's key."""
    unique = _uniquely_constrained_properties()
    expr = _DIRECTION_RE.sub("", key).strip()
    for _ in range(8):
        prop = re.fullmatch(r"(\w+)\s*\.\s*(\w+)", expr)
        if prop is not None:
            holder = _bindings_before(bindings, before).get(prop.group(1))
            return prop.group(2) in unique and holder is not None
        if re.fullmatch(r"elementId\(\s*\w+\s*\)", expr):
            return True
        if not _BARE_NAME_RE.fullmatch(expr):
            return False
        aliased = _aliased_expression(code, before, expr)
        if aliased is None:
            return False
        expr, before = aliased
    return False


def _projection_columns(code: str, depths: list[int], start: int, end: int) -> set[str] | None:
    """The column names a `WITH`/`RETURN` projects, or `None` for `*`."""
    columns: set[str] = set()
    for _offset, raw in _split_top_level(code, depths, start, end):
        item = raw.removeprefix("DISTINCT ")
        if item == "*":
            return None
        alias = _ALIAS_RE.match(item)
        columns.add(alias.group("alias") if alias else item)
    return columns


def _cut_is_total(
    code: str,
    depths: list[int],
    projection: _ClauseToken,
    order: _ClauseToken,
    cut: int,
    *,
    bindings: list[_Binding],
) -> bool:
    """An `ORDER BY` before a cut is total when its last key is a unique key, or
    when its keys name every column the projection carries: rows equal in every
    column are one row to the caller, whichever of them the cut keeps."""
    keys = [k for _o, k in _split_top_level(code, depths, order.end, cut)]
    if not keys:
        return False
    if _uniquely_keyed(keys[-1], code, order.start, bindings):
        return True
    columns = _projection_columns(code, depths, projection.end, order.start)
    bare_keys = {_DIRECTION_RE.sub("", k).strip() for k in keys}
    return columns is not None and columns <= bare_keys


def _order_findings(rel: str, code: str) -> list[Finding]:
    """A `LIMIT` with no `ORDER BY` in its projection, and an `ORDER BY` directly
    before a `LIMIT` whose order is not total: the store cuts to a count, and the
    rows it keeps must follow from the data, never from store order."""
    depths = _depths(code)
    tokens = _clause_tokens(code, depths)
    bindings = _node_bindings(code, depths)
    findings: list[Finding] = []
    for index, limit in enumerate(tokens):
        if limit.kw != "LIMIT":
            continue
        same = [t for t in tokens[:index] if t.depth == limit.depth]
        projection = next((t for t in reversed(same) if t.kw in _PROJECTION_KWS), None)
        clause = [t for t in same if projection is not None and t.start > projection.start]
        order = next((t for t in clause if t.kw == "ORDER BY"), None)
        if projection is None or order is None:
            findings.append(
                Finding(
                    rel,
                    _line_of(code, limit.start),
                    1,
                    "cypher-lint-limit-without-order",
                    "LIMIT cuts rows its projection never ordered, so which rows are kept "
                    "follows store order",
                    fix="order the projection by the ranking, ending in a unique key, "
                    "before the LIMIT",
                    doc=DOC_PRUNE,
                )
            )
            continue
        cut = next((t.start for t in clause if t.kw == "SKIP"), limit.start)
        if not _cut_is_total(code, depths, projection, order, cut, bindings=bindings):
            findings.append(
                Finding(
                    rel,
                    _line_of(code, order.start),
                    1,
                    "cypher-lint-order-not-total",
                    "the ORDER BY before this LIMIT ends in a key that is not unique, so "
                    "rows tied on every key are cut in store order",
                    fix="end the ORDER BY in a unique key (an id or a uniquely constrained "
                    "property), or order by every projected column",
                    doc=DOC_PRUNE,
                )
            )
    return findings


def _header_comment_findings(rel: str, text: str) -> list[Finding]:
    """Every .cypher file must begin with a header comment stating its purpose."""
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("//", "/*")):
            return []
        return [
            Finding(
                path=rel,
                line=line_no,
                col=1,
                rule="cypher-lint-missing-header-comment",
                message="a .cypher file must begin with a header comment stating its purpose",
                fix="add a header comment stating query purpose",
                doc=DOC_QUERY_FILES,
            )
        ]
    return []


def _documented_binding_findings(rel: str, path: Path, code: str) -> list[Finding]:
    """A file with a variable-length pattern whose stem names no capped-traversal row.

    Every cap rule degrades to nothing on an unregistered stem, so that is never a silent
    skip. DDL is out of scope."""
    if _is_ddl_file(path):
        return []
    if not (_REL_QUANTIFIER_RE.search(code) or _QPP_QUANTIFIER_RE.search(code)):
        return []
    if _capped_for(path) is not None:
        return []
    return [
        Finding(
            path=rel,
            line=1,
            col=1,
            rule="cypher-lint-unregistered-capped-query",
            message=(
                f"{path.name!r} carries a variable-length path but its stem names no "
                "policy.capped_traversals row, so its cap and config key are never checked"
            ),
            fix="register the query under policy.capped_traversals, named for this "
            "file's stem, with its hard cap and config key",
            doc=DOC_BOUND,
        )
    ]


def _one_statement_findings(rel: str, path: Path, code: str) -> list[Finding]:
    """A query file holds exactly one statement.

    DDL files (`policy.ddl_globs`) are exempt: Neo4j takes one DDL statement per call, and
    the checks split such a file statement by statement.
    """
    if _is_ddl_file(path):
        return []
    parts = [p.strip() for p in code.split(";")]
    non_empty = [p for p in parts if p]
    if len(non_empty) > 1:
        first_semi = code.find(";")
        line_no = code[:first_semi].count("\n") + 1
        return [
            Finding(
                path=rel,
                line=line_no,
                col=1,
                rule="cypher-lint-multiple-statements",
                message="a .cypher file must contain exactly one statement",
                fix="split into separate .cypher files",
                doc=DOC_QUERY_FILES,
            )
        ]
    return []


# --- 13. Every read has an owner ------------------------------------------------


def _read_owner_findings(rel: str, path: Path) -> list[Finding]:
    """A read file (`policy.read_globs`) whose stem names no `policy.read_owners` entry.

    Off while `read_globs` is empty; with it set and no owners listed, every read is a finding."""
    policy = _schema().policy
    if not matches_any(path, policy.read_globs):
        return []
    if documented_name_for(path, policy.read_owners) is not None:
        return []
    return [
        Finding(
            rel,
            1,
            1,
            "cypher-lint-read-without-owner",
            f"policy.read_owners has no entry for {path.stem!r}",
            fix="add the file's stem to policy.read_owners, or remove the file",
            doc=DOC_READ_OWNERS,
        )
    ]


def read_register_findings(paths: Sequence[str | Path], schema: GraphSchema) -> list[Finding]:
    """The register's own half: an owner entry that no checked read file's stem binds to.

    Only meaningful when `paths` holds every read file; the CLI passes whatever it was given."""
    policy = schema.policy
    if not policy.read_globs:
        return []
    reads = [p for p in expand_targets(paths) if matches_any(p, policy.read_globs)]
    stems = {normalize_query_name(p.stem) for p in reads}
    return [
        Finding(
            "policy.read_owners",
            1,
            1,
            "cypher-lint-owner-row-without-read",
            f"the read owner {owner!r} names no read file among the checked targets",
            fix="name the stem of an existing read file, or remove the entry",
            doc=DOC_READ_OWNERS,
        )
        for owner in policy.read_owners
        if normalize_query_name(owner) not in stems
    ]


def check_file(path: Path, root: Path | None = None) -> list[Finding]:
    """Every finding for one file, under the schema `using_schema` bound."""
    rel = _display_path(path, root)
    text = path.read_text(encoding="utf-8")
    # Comments blanked first and kept for the formatting rule, which reads the comments
    # above quantified paths. Every other rule reads `code`, which also blanks string
    # interiors (both quote styles) so a keyword or type inside a value never trips it.
    comments_blanked = _blank_comments(text)
    code = _blank_double_quoted_strings(_blank_single_quoted_strings(comments_blanked))
    findings = [
        *_header_comment_findings(rel, text),
        *_one_statement_findings(rel, path, code),
        *_documented_binding_findings(rel, path, code),
        *_bound_findings(rel, path, code),
        *_merge_key_findings(rel, path, code),
        *_no_create_or_delete_findings(rel, path, code),
        *_write_path_findings(rel, path, code),
        *_anchor_findings(rel, path, code),
        *_relationship_type_findings(rel, code),
        *_no_templating_findings(rel, code),
        *_formatting_findings(rel, code, raw=text),
        *_set_property_findings(rel, code),
        *_shared_set_findings(rel, code, comments_blanked),
        *_vector_return_findings(rel, path, code),
        *_order_findings(rel, code),
        *_read_owner_findings(rel, path),
    ]
    return sorted(findings, key=sort_key)


def _display_path(path: Path, root: Path | None) -> str:
    """`path` relative to `root` (the working directory by default) when it lies under it."""
    base = (Path.cwd() if root is None else root).resolve()
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def check(
    paths: Sequence[str | Path], schema: GraphSchema, root: Path | None = None
) -> list[Finding]:
    """Every finding for every `.cypher` file named or found under a named directory."""
    with using_schema(schema):
        findings = [f for p in expand_targets(paths) for f in check_file(p, root)]
    findings += read_register_findings(paths, schema)
    return sorted(findings, key=sort_key)
