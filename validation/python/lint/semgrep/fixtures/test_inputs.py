"""Fixtures for semgrep/test_inputs.yml: positive and negative cases per rule
(`--test` ignores `paths:`, so `paths.include` itself is not exercised here)."""


def refused(response, rows):
    # ruleid: kit.tests.selection-by-response-position
    first = response.json()[0]
    # ruleid: kit.tests.selection-by-response-position
    last = response.json()[-1]
    # ruleid: kit.tests.selection-by-response-position
    head = next(iter(response.json()))
    # ruleid: kit.tests.selection-by-response-position
    fallback = next(iter(response.json()), None)
    # ruleid: kit.tests.selection-by-sorted-id
    ordered = sorted(rows, key=lambda row: row["id"])
    # ruleid: kit.tests.selection-by-sorted-id
    named = sorted(rows, key=lambda row: str(row["id"]))
    # ruleid: kit.tests.selection-by-sorted-id
    rows.sort(key=lambda row: row["id"])
    # ruleid: kit.tests.selection-by-sorted-id
    low = min(rows, key=lambda row: row["id"])
    # ruleid: kit.tests.selection-by-sorted-id
    high = max(rows, key=lambda row: str(row["id"]))
    return first, last, head, fallback, ordered, named, low, high


def admitted(response, rows, key):
    # ok: kit.tests.selection-by-response-position
    items = response.json()["items"]
    # ok: kit.tests.selection-by-response-position
    keyed = response.json()[key]
    # ok: kit.tests.selection-by-sorted-id
    by_code = sorted(rows, key=lambda row: row["code"])
    # ok: kit.tests.selection-by-sorted-id
    by_label = min(rows, key=lambda row: row["label"])
    return items, keyed, by_code, by_label
