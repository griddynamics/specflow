"""Render a ``Diagram`` as Markdown (implemented by the converter; stdlib only).

The output is deterministic: the same ``Diagram`` always gives byte-equal text, so the content hash dedupe works.
"""

from __future__ import annotations

import re

from file_parsers.diagrams.model import (
    MAX_MARKDOWN_BYTES,
    MAX_MARKDOWN_FILE_BYTES,
    Diagram,
    DiagramDecodeError,
    Page,
    Role,
    Shape,
)

_SPECIAL = re.compile(r"[\\`*\[\]<>]")
_LONE_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")
_ENTITY = re.compile(r"&(?=#?[0-9A-Za-z]+;)")
_BLOCK_START = re.compile(r"^(?:[#>+=~-]|\d+(?=[.)]))")
_UNLABELLED = "(unlabelled shape)"
MAX_LABEL = 300
_PATH = " \u203a "  # a single right-pointing angle quotation mark
_ANCESTORS = 3  # a group title names the group and its nearest ancestors, three names in all


def _esc(text: str) -> str:
    """Cap at ``MAX_LABEL`` characters, then escape what could start a heading, list, link, emphasis, code or entity."""

    if len(text) > MAX_LABEL:
        text = text[: MAX_LABEL - 1] + "…"
    text = _SPECIAL.sub(lambda m: "\\" + m.group(0), text)
    text = _LONE_UNDERSCORE.sub(r"\\_", text)
    text = _ENTITY.sub(r"\\&", text)
    match = _BLOCK_START.match(text)
    if match is None:
        return text
    if match.group(0).isdigit():  # "1. x" starts a list: escape the dot, not the digit
        return f"{match.group(0)}\\{text[match.end() :]}"
    return "\\" + text


def _head(label: str) -> tuple[str, str]:
    head, _, tail = label.partition(" · ")
    return head, tail


def _flat(value: str) -> str:
    return " ".join(value.split())


def _props(shape: Shape, indent: str) -> list[str]:
    return [f"{indent}- {_esc(_flat(k))}: {_esc(_flat(v))}" for k, v in shape.props if _flat(v)]


def _bullet(shape: Shape) -> list[str]:
    head, tail = _head(shape.label)
    line = f"- **{_esc(head)}** ({shape.role.value})"
    if tail:
        line += f": {_esc(tail)}"
    return [line, *_props(shape, "  ")]


def _group_title(shape: Shape, groups: dict[str, Shape]) -> str:
    """The group and its nearest ancestors (three names) joined by ``_PATH``, led by ``…`` when the path is longer."""

    names = [shape.label]
    seen = {shape.id}
    parent = shape.group
    while parent is not None and parent in groups and parent not in seen and len(names) <= _ANCESTORS:
        seen.add(parent)
        names.append(groups[parent].label)
        parent = groups[parent].group
    more = len(names) > _ANCESTORS
    titles = [_esc(n) for n in reversed(names[:_ANCESTORS])]
    return _PATH.join(["…", *titles] if more else titles)


def _shape_lines(shapes: list[Shape], groups: dict[str, Shape], level: int) -> list[str]:
    lines = [ln for s in shapes if s.role not in (Role.GROUP, Role.NOTE) and s.group is None for ln in _bullet(s)]
    members: dict[str, list[Shape]] = {}
    for member in shapes:
        if member.group is not None and member.role not in (Role.GROUP, Role.NOTE):
            members.setdefault(member.group, []).append(member)
    for group in (s for s in shapes if s.role is Role.GROUP):
        lines.append(f"{'#' * level} Group: {_group_title(group, groups)}")
        lines.extend(f"- {_esc(_flat(k))}: {_esc(_flat(v))}" for k, v in group.props if _flat(v))
        lines.extend(ln for member in members.get(group.id, ()) for ln in _bullet(member))
    return lines


def _arrow_names(page: Page) -> dict[str, str]:
    """Shape id to its name in an arrow line: the label head, made unique on the page by the nearest group (``Review
    (in Design)``), then by its position among the equal names (``Review #2``, in the order of the shapes)."""

    labels = {s.id: s.label for s in page.shapes}
    heads = {s.id: _head(s.label)[0] for s in page.shapes}
    taken: dict[str, int] = {}
    for head in heads.values():
        taken[head] = taken.get(head, 0) + 1
    names: dict[str, str] = {}
    for shape in page.shapes:
        head = heads[shape.id]
        name = _esc(head)
        if taken[head] > 1 and shape.group in labels:
            name += f" (in {_esc(_head(labels[shape.group or ''])[0])})"
        names[shape.id] = name
    count: dict[str, int] = {}
    for name in names.values():
        count[name] = count.get(name, 0) + 1
    seen: dict[str, int] = {}
    for shape in page.shapes:
        name = names[shape.id]
        if count[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            names[shape.id] = f"{name} #{seen[name]}"
    return names


def _arrow_lines(page: Page) -> list[str]:
    names = _arrow_names(page)
    lines = []
    for arrow in page.arrows:
        src = names.get(arrow.source or "", _UNLABELLED)
        dst = names.get(arrow.target or "", _UNLABELLED)
        line = f"- {src} → {dst}"
        if arrow.label:
            line += f": {_esc(arrow.label)}"
        lines.append(line)
    return lines


def _page_lines(page: Page, level: int) -> list[str]:
    """The sections of one page; ``level`` is the heading depth of ``Shapes``, ``Arrows`` and ``Notes``."""

    mark = "#" * level
    groups = {s.id: s for s in page.shapes if s.role is Role.GROUP}
    body: list[str] = []
    if len(page.layers) > 1:
        loose = [s for s in page.shapes if s.layer is None]
        body.extend(_shape_lines(loose, groups, level + 1))
        for layer in page.layers:
            members = [s for s in page.shapes if s.layer == layer]
            if members:
                body.append(f"{mark}# Layer: {_esc(layer)}")
                body.extend(_shape_lines(members, groups, level + 2))
    else:
        body.extend(_shape_lines(list(page.shapes), groups, level + 1))
    out: list[str] = []
    if body:
        out += [f"{mark} Shapes", *body, ""]
    arrows = _arrow_lines(page)
    if arrows:
        out += [f"{mark} Arrows", *arrows, ""]
    notes = [ln for s in page.shapes if s.role is Role.NOTE for ln in _note(s)]
    if notes:
        out += [f"{mark} Notes", *notes, ""]
    return out


def _note(shape: Shape) -> list[str]:
    return [f"- {_esc(shape.label)}", *_props(shape, "  ")]


def _index(page: Page, diagram: Diagram) -> int:
    for i, candidate in enumerate(diagram.pages, 1):
        if candidate is page or candidate == page:
            return i
    return 1


def _finish(lines: list[str], limit: int) -> str:
    text = "\n".join(lines).rstrip("\n") + "\n"
    if len(text.encode()) > limit:
        raise DiagramDecodeError("too_large")
    return text


def page_title(diagram: Diagram, page: Page) -> str:
    """``<diagram name> · <page name>``, with an empty or repeated part left out (a one-page diagram is often named
    after its page)."""

    parts = [diagram.name.strip(), page.name.strip()]
    return " · ".join(dict.fromkeys(p for p in parts if p))


def to_markdown(page: Page, diagram: Diagram) -> str:
    """The Markdown of one page (one document of a link import), as in the plan section 3::

        # <diagram name> · <page name>
        Source: diagrams.net · page <n> of <m>

        ## Shapes
        - **Design gate** (decision): "Yes, I reviewed the design"
        ### Group: <container label>
        ## Arrows
        - Design → Design gate
        ## Notes

    ``n`` is the 1-based index of ``page`` in ``diagram.pages`` and ``m`` their count; a ``Page`` that is not in
    ``diagram.pages`` is never passed. Empty sections are left out. A page with more than one layer gets
    ``### Layer: <name>`` sub-sections. Every label is cut at ``MAX_LABEL`` characters, a group title names three levels
    (``_PATH`` between them) and text over ``MAX_MARKDOWN_BYTES`` raises ``DiagramDecodeError("too_large")``.
    A ``link`` property stays plain text, never a Markdown link. Ends with one newline.
    """

    title = _esc(page_title(diagram, page)) or "Diagram"
    head = [f"# {title}", f"Source: diagrams.net · page {_index(page, diagram)} of {len(diagram.pages)}", ""]
    return _finish([*head, *_page_lines(page, 2)], MAX_MARKDOWN_BYTES)


def file_markdown(diagram: Diagram) -> str:
    """The Markdown of a whole file (one document of an upload): ``# <diagram name>`` and, per page, a ``## <page
    name>`` section holding that page's shapes, arrows and notes. Same determinism as ``to_markdown``."""

    count = len(diagram.pages)
    lines = [
        f"# {_esc(diagram.name) or 'Diagram'}",
        f"Source: diagrams.net · {count} page{'s' if count != 1 else ''}",
        "",
    ]
    for page in diagram.pages:
        lines += [f"## {_esc(page.name) or 'Page'}", ""]
        lines += _page_lines(page, 3)
    return _finish(lines, MAX_MARKDOWN_FILE_BYTES)
