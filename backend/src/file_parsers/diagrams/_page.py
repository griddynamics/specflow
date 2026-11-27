"""Build a ``Page`` from an ``<mxGraphModel>`` (stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import override
from xml.etree import ElementTree as ET

from file_parsers.diagrams._xml import WRAPPERS
from file_parsers.diagrams.model import Arrow, Page, Role, Shape

_NON_PROPS = ("label", "placeholders", "id")
_BREAK_TAGS = frozenset({"br", "div", "p", "li", "tr", "ul", "ol", "table", "hr", "h1", "h2", "h3", "h4", "h5", "h6"})
_SKIP_TAGS = frozenset({"script", "style"})
_MAX_CHAIN = 200
# draw.io flowchart shapes, by the whole ``shape=`` value; terminator, start_1 and start_2 stay ``step``
_SHAPE_ROLES = {
    "mxgraph.flowchart.decision": Role.DECISION,
    "mxgraph.flowchart.database": Role.STORE,
    "mxgraph.flowchart.stored_data": Role.STORE,
    "mxgraph.flowchart.document": Role.DOCUMENT,
    "mxgraph.flowchart.multi-document": Role.DOCUMENT,
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = [""]
        self._skip = 0

    def _break(self) -> None:
        if self.parts[-1]:
            self.parts.append("")

    @override
    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self._break()

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BREAK_TAGS:
            self._break()

    @override
    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts[-1] += data


def clean_label(value: str, *, html: bool) -> str:
    """Strip HTML (only when ``html=1``), unescape entities, turn line breaks into `` · `` and collapse whitespace."""

    if html:
        parser = _Text()
        parser.feed(value)
        parser.close()
        segments = parser.parts
    else:
        segments = value.splitlines()
    return " · ".join(part for part in (" ".join(seg.split()) for seg in segments) if part)


def _style(style: str) -> tuple[dict[str, str], set[str]]:
    pairs: dict[str, str] = {}
    names: set[str] = set()
    for token in style.split(";"):
        key, sep, value = token.partition("=")
        key = key.strip().lower()
        if not key:
            continue
        if sep:
            pairs[key] = value.strip().lower()
        else:
            names.add(key)
    return pairs, names


def _role(pairs: dict[str, str], names: set[str], *, is_parent: bool) -> Role:
    shape = pairs.get("shape", "")
    names = names | {shape.rsplit(".", 1)[-1]}
    if is_parent or "swimlane" in names or pairs.get("container") == "1":
        return Role.GROUP
    if shape in _SHAPE_ROLES:
        return _SHAPE_ROLES[shape]
    checks = (
        (Role.DECISION, "rhombus" in names),
        (Role.STORE, "datastore" in names or any(n.startswith("cylinder") for n in names)),
        (Role.DOCUMENT, "document" in names or "note" in names),
        (Role.ACTOR, "actor" in names or "umlactor" in names),
        (Role.NOTE, "text" in names),
    )
    return next((role for role, hit in checks if hit), Role.STEP)


def _num(value: str | None) -> float:
    try:
        return float(value) if value is not None else 0.0
    except ValueError:
        return 0.0


@dataclass(slots=True)
class _Cell:
    id: str
    parent: str
    source: str
    target: str
    vertex: bool
    edge: bool
    html: bool
    pairs: dict[str, str]
    names: set[str]
    value: str
    x: float
    y: float
    props: tuple[tuple[str, str], ...]
    index: int
    label: str = field(default="")


def _cells(model: ET.Element) -> list[_Cell]:
    root = model.find("root")
    out: list[_Cell] = []
    if root is None:
        return out
    for index, child in enumerate(root):
        wrapper = child if child.tag in WRAPPERS else None
        cell = child.find("mxCell") if wrapper is not None else child
        if cell is None or cell.tag != "mxCell":
            continue
        meta = wrapper if wrapper is not None else cell
        cell_id = meta.get("id")
        if not cell_id:
            continue
        pairs, names = _style(cell.get("style", ""))
        geometry = cell.find("mxGeometry")
        value = meta.get("label", "") if wrapper is not None else cell.get("value", "")
        props = tuple((k, v) for k, v in wrapper.attrib.items() if k not in _NON_PROPS) if wrapper is not None else ()
        out.append(
            _Cell(
                id=cell_id,
                parent=cell.get("parent", ""),
                source=cell.get("source", ""),
                target=cell.get("target", ""),
                vertex=cell.get("vertex") == "1",
                edge=cell.get("edge") == "1",
                html=pairs.get("html") == "1",
                pairs=pairs,
                names=names,
                value=value,
                x=_num(geometry.get("x")) if geometry is not None else 0.0,
                y=_num(geometry.get("y")) if geometry is not None else 0.0,
                props=props,
                index=index,
                label=clean_label(value, html=pairs.get("html") == "1"),
            )
        )
    return out


def _strip_blanks(elem: ET.Element) -> None:
    for node in elem.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None


def _original(page_id: str, name: str, model: ET.Element) -> bytes:
    root = model.find("root")
    if root is not None:
        for cell in list(root):
            inner = cell if cell.tag == "mxCell" else cell.find("mxCell")
            cleared = not cell.attrib if cell.tag in WRAPPERS else inner is not None and not inner.attrib
            if cleared:
                root.remove(cell)  # a cell or wrapper cleared by the cap
    _strip_blanks(model)
    file = ET.Element("mxfile")
    ET.SubElement(file, "diagram", {"id": page_id, "name": name}).append(model)
    return ET.tostring(file, encoding="unicode").encode("utf-8")


def _layer_names(cells: list[_Cell]) -> dict[str, str]:
    root_ids = {c.id for c in cells if not c.parent}
    layers = [c for c in cells if c.parent in root_ids and not c.vertex and not c.edge]
    return {c.id: c.label or f"Layer {i}" for i, c in enumerate(layers, 1)}


def _place(cell: _Cell, vertices: dict[str, _Cell], shape_ids: set[str]) -> tuple[float, float, str | None, _Cell]:
    """Absolute ``x``, ``y``, the nearest containing ``Shape`` and the topmost ancestor of ``cell``."""

    x, y = cell.x, cell.y
    group: str | None = None
    top = cell
    seen = {cell.id}
    for _ in range(_MAX_CHAIN):
        up = vertices.get(top.parent)
        if up is None or up.id in seen:
            break
        seen.add(up.id)
        x += up.x
        y += up.y
        if group is None and up.id in shape_ids:
            group = up.id
        top = up
    return x, y, group, top


def build_page(page_id: str, name: str, model: ET.Element | None) -> Page:
    model = model if model is not None else ET.Element("mxGraphModel")
    cells = _cells(model)
    vertices = {c.id: c for c in cells if c.vertex and not c.edge}
    edges = [c for c in cells if c.edge]
    edge_ids = {c.id for c in edges}
    layer_names = _layer_names(cells)
    parents = {c.parent for c in vertices.values() if c.parent in vertices}

    edge_labels: dict[str, list[str]] = {}
    nodes: list[tuple[_Cell, Role]] = []
    for cell in vertices.values():
        if cell.parent in edge_ids:
            if cell.label:
                edge_labels.setdefault(cell.parent, []).append(cell.label)
        elif cell.label:
            nodes.append((cell, _role(cell.pairs, cell.names, is_parent=cell.id in parents)))
    shape_ids = {cell.id for cell, _ in nodes}

    placed: list[tuple[float, float, int, Shape]] = []
    for cell, role in nodes:
        x, y, group, top = _place(cell, vertices, shape_ids)
        layer = layer_names.get(top.parent) if len(layer_names) > 1 else None
        placed.append((y, x, cell.index, Shape(cell.id, cell.label, role, group, layer, x, y, cell.props)))
    placed.sort(key=lambda t: t[:3])

    arrows = tuple(
        Arrow(
            edge.source if edge.source in shape_ids else None,
            edge.target if edge.target in shape_ids else None,
            " · ".join(filter(None, [edge.label, *edge_labels.get(edge.id, [])])),
        )
        for edge in edges
    )
    names = tuple(layer_names.values())
    named_single = len(names) == 1 and any(c.label for c in cells if c.id in layer_names)
    return Page(
        id=page_id,
        name=name,
        shapes=tuple(s for *_, s in placed),
        arrows=arrows,
        layers=names if len(names) > 1 or named_single else (),
        original=_original(page_id, name, model),
    )
