"""ADF builders: description layouts and the change comment."""

from __future__ import annotations

from jira.adf import QUOTE_LIMIT, comment_doc, description_doc


def kinds(doc):
    return [block["type"] for block in doc["content"]]


def texts(node):
    if node.get("type") == "text":
        return [node["text"]]
    return [text for child in node.get("content", []) for text in texts(child)]


def paragraph_texts(doc):
    return ["".join(texts(block)) for block in doc["content"] if block["type"] == "paragraph"]


STORY = {
    "text": "First paragraph.\n\nSecond paragraph.",
    "release": "R1",
    "specs": [{"id": "SPEC::a", "name": "Spec A"}],
    "done_when": [],
}


def test_story_layout_in_order():
    doc = description_doc("story", STORY, story_id="STORY::x::y")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert kinds(doc) == ["paragraph", "paragraph", "paragraph", "heading", "bulletList", "paragraph"]
    assert paragraph_texts(doc) == ["First paragraph.", "Second paragraph.", "Release: R1", "Specflow: STORY::x::y"]
    assert texts(doc["content"][4]) == ["Spec A (SPEC::a)"]
    last = doc["content"][-1]["content"][1]
    assert last["marks"] == [{"type": "code"}]


def test_story_empty_parts_are_left_out():
    doc = description_doc("story", {"text": None, "release": None, "specs": []}, story_id="STORY::x::y")
    assert paragraph_texts(doc) == ["Specflow: STORY::x::y"]
    assert kinds(doc) == ["paragraph"]
    assert description_doc("story", {})["content"] == []


def test_subtask_layout_in_order_with_todo_items():
    content = {
        "text": "Do the work.",
        "done_when": ["tests pass", "docs updated"],
        "phase": "Build",
        "specs": [{"id": "SPEC::a", "name": "SPEC::a"}],
        "task_id": "TASK::1_R2",
    }
    doc = description_doc("subtask", content)
    assert kinds(doc) == ["paragraph", "heading", "taskList", "paragraph", "heading", "bulletList", "paragraph"]
    heading = doc["content"][1]
    assert heading["attrs"] == {"level": 3}
    assert texts(heading) == ["Done when"]
    items = doc["content"][2]["content"]
    assert [item["attrs"]["state"] for item in items] == ["TODO", "TODO"]
    assert len({item["attrs"]["localId"] for item in items}) == 2
    assert [texts(item) for item in items] == [["tests pass"], ["docs updated"]]
    assert paragraph_texts(doc) == ["Do the work.", "Phase: Build", "Specflow: TASK::1_R2"]
    assert texts(doc["content"][5]) == ["SPEC::a (SPEC::a)"]


def test_subtask_empty_parts_are_left_out():
    doc = description_doc("subtask", {"text": "", "done_when": [], "phase": None, "specs": [], "task_id": "T"})
    assert kinds(doc) == ["paragraph"]
    assert paragraph_texts(doc) == ["Specflow: T"]


def test_comment_has_lead_and_one_bullet_per_change():
    doc = comment_doc("Lead.", ['Summary: "a" → "b"', "Label: added x"])
    assert kinds(doc) == ["paragraph", "bulletList"]
    assert [texts(item) for item in doc["content"][1]["content"]] == [['Summary: "a" → "b"'], ["Label: added x"]]


def test_comment_without_items_is_the_lead_only():
    assert kinds(comment_doc("Lead.", [])) == ["paragraph"]


def test_comment_quote_is_cut_at_2000_characters():
    long = "x" * 5000
    doc = comment_doc("Lead.", [("Description changed. New text:", long)])
    entry = doc["content"][1]["content"][0]["content"]
    assert [block["type"] for block in entry] == ["paragraph", "blockquote"]
    quote = texts(entry[1])[0]
    assert len(quote) == QUOTE_LIMIT
    assert quote.endswith("…")
    assert quote[:-1] == "x" * (QUOTE_LIMIT - 1)


def test_comment_quote_at_the_limit_is_kept_whole():
    exact = "y" * QUOTE_LIMIT
    entry = comment_doc("L", [("t", exact)])["content"][1]["content"][0]["content"]
    assert texts(entry[1]) == [exact]
