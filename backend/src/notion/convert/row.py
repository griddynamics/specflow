"""A database row as a document: its title, a table of its properties, then its body (N3)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from drive.convert._md import as_list, as_map, escape_inline, one_line, table_md, title_of
from notion.convert.result import ConvertedPage
from notion.convert.scrub import HIDDEN, scrub_emails
from notion.model import plain_text

_SKIPPED: Final = frozenset({"title"})


def _name(item: Any) -> str:
    """The display name of a user, a file or a related page (never its id, address or e-mail)."""

    data = as_map(item)
    return str(data.get("name") or data.get("title") or "") if "name" in data or "title" in data else ""


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _date(value: Any) -> str:
    data = as_map(value)
    start, end = str(data.get("start") or ""), str(data.get("end") or "")
    return f"{start} to {end}" if start and end else start


def _unique_id(value: Any) -> str:
    data = as_map(value)
    number = _number(data.get("number"))
    return f"{data.get('prefix')}-{number}" if data.get("prefix") and number else number


def _listed(value: Any) -> str:
    names = (_text(item) if "type" in as_map(item) else _name(item) for item in as_list(value))
    return ", ".join(name for name in names if name)


def _computed(value: Any) -> str:
    """A formula or rollup: its value sits under the key of its own type."""

    data = as_map(value)
    inner = str(data.get("type", ""))
    return _text({"type": inner, inner: data.get(inner)}) if inner else ""


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _checkbox(value: Any) -> str:
    return "Yes" if value is True else "No" if value is False else ""


_READERS: Final[dict[str, Callable[[Any], str]]] = {
    "rich_text": plain_text,
    "title": plain_text,
    "select": lambda v: str(as_map(v).get("name") or ""),
    "status": lambda v: str(as_map(v).get("name") or ""),
    "email": lambda v: HIDDEN if v else "",
    "url": _string,
    "phone_number": _string,
    "created_time": _string,
    "last_edited_time": _string,
    "string": _string,
    "number": _number,
    "checkbox": _checkbox,
    "boolean": _checkbox,
    "date": _date,
    "unique_id": _unique_id,
    "created_by": _name,
    "last_edited_by": _name,
    "multi_select": _listed,
    "relation": _listed,
    "files": _listed,
    "people": _listed,
    "array": _listed,
    "formula": _computed,
    "rollup": _computed,
}


def _text(prop: Mapping[str, Any]) -> str:
    """The text of one property value from Notion's property map ("" for a type that has none)."""

    kind = str(prop.get("type", ""))
    reader = _READERS.get(kind)
    return reader(prop.get(kind)) if reader else ""


def render_row(title: str, properties: Mapping[str, Any], body: ConvertedPage) -> str:
    """The document of a database row: ``# <title>``, a table of its properties (Notion's property map; people as
    display names only; an ``email`` property is shown as "(e-mail hidden)"; relations and files by name; no user id),
    then the converted ``body``."""

    rows = [["Property", "Value"]]
    for name in sorted(properties, key=lambda n: (str(n).casefold(), str(n))):
        prop = as_map(properties[name])
        value = one_line(_text(prop)) if prop.get("type") not in _SKIPPED else ""
        if value:
            rows.append([escape_inline(one_line(str(name))), escape_inline(value)])
    parts = [f"# {title_of(title)}"]
    if len(rows) > 1:
        parts.append(table_md(rows))
    if body.markdown.strip():
        parts.append(body.markdown.strip("\n"))
    return scrub_emails("\n\n".join(parts)) + "\n"
