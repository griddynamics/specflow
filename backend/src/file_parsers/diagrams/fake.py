"""A small fixed diagram for the tests of the callers (the importer, the upload conversion): two pages, no XML."""

from __future__ import annotations

from file_parsers.diagrams.model import Arrow, Diagram, Page, Role, Shape


def fake_diagram() -> Diagram:
    """``Flow`` with page ``Main`` (three shapes, two arrows) and page ``Second`` (one shape, no arrow); ``capped`` is
    false. Each page has its own ``original`` so the two documents hash differently."""

    main = Page(
        id="p1",
        name="Main",
        shapes=(
            Shape("a", "Start", Role.STEP, None, None, 0.0, 0.0),
            Shape("b", "Approved?", Role.DECISION, None, None, 0.0, 100.0),
            Shape("c", "Archive", Role.STORE, None, None, 0.0, 200.0, (("owner", "ops"),)),
        ),
        arrows=(Arrow("a", "b", ""), Arrow("b", "c", "yes")),
        original=b'<mxfile><diagram id="p1" name="Main"></diagram></mxfile>',
    )
    second = Page(
        id="p2",
        name="Second",
        shapes=(Shape("d", "Note", Role.NOTE, None, None, 0.0, 0.0),),
        arrows=(),
        original=b'<mxfile><diagram id="p2" name="Second"></diagram></mxfile>',
    )
    return Diagram(name="Flow", pages=(main, second))
