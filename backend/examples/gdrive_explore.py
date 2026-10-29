"""Explore a shared Drive folder read-only and check it against the Drive source UX specs.

Needs only google-auth and requests, which the production image already has
(through google-cloud-storage), so it runs in a pod without a deploy:

    kubectl exec -i -n <namespace> <api pod> -- python - "<folder link>" < examples/gdrive_explore.py
    kubectl exec -i -n <namespace> <api pod> -- python - "<folder link>" --record \
        < examples/gdrive_explore.py > recorded-listing.json

Without --record it prints a report: who Drive sees, the folder walk (all
levels, time, calls), the summary as the modal would show it, the raw Drive
types, and what Drive answers for a file id and for an unknown id. With
--record it prints only a recorded folder listing (JSON) with generated names
and no real names, ids, owners or emails. Offline check of the link and
category rules, no Google call:

    uv run python examples/gdrive_explore.py --self-test

Reads metadata only; never changes Drive.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from http import HTTPStatus
from typing import Any, cast

import google.auth
from google.auth.transport.requests import AuthorizedSession

DRIVE = "https://www.googleapis.com/drive/v3"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
GOOGLE = "application/vnd.google-apps."
FOLDER = GOOGLE + "folder"
SHORTCUT = GOOGLE + "shortcut"
ID = r"[A-Za-z0-9_-]{10,}"
UNKNOWN_ID = "A" * 33
MAX_ATTEMPTS = 6
RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFF = threading.Event()  # never set: `wait(seconds)` is an interruptible backoff pause

# DATA-GDUI-0002: accepted folder link forms
LINK_FORMS = [
    re.compile(rf"https://drive\.google\.com/drive/folders/({ID})/?"),
    re.compile(rf"https://drive\.google\.com/drive/u/\d+/folders/({ID})/?"),
]
LINK_CASES = [
    (
        "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345?usp=sharing",
        True,
    ),
    (
        "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345?usp=share_link",
        True,
    ),
    (
        "https://drive.google.com/drive/u/1/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/",
        True,
    ),
    ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz012345", True),
    ("  abcdefghij", True),
    ("abcdefghi", False),
    ("https://evil.example/drive/folders/1AbCdEfGhIjKlMnOp", False),
    ("https://docs.google.com/document/d/1abcdefghijk/edit", False),
    ("drive.google.com/abc", False),
]

# DATA-GDUI-0001: Drive type -> (category label, processing class)
CLASS_ORDER = {"processed": 0, "stored only": 1, "skipped": 2}
FLAGS = {"stored only": " (stored, not read yet)", "skipped": " (skipped)"}
CATEGORIES = {
    GOOGLE + "document": ("Google Docs", "processed"),
    GOOGLE + "presentation": ("Google Slides", "processed"),
    GOOGLE + "spreadsheet": ("Google Sheets", "processed"),
    "application/pdf": ("PDF", "processed"),
    "application/msword": ("Word documents", "stored only"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "Word documents",
        "stored only",
    ),
    "application/vnd.ms-powerpoint": ("PowerPoint files", "stored only"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "PowerPoint files",
        "stored only",
    ),
    "application/vnd.ms-excel": ("Excel files", "stored only"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        "Excel files",
        "stored only",
    ),
    SHORTCUT: ("Shortcuts", "skipped"),
}

ListChildren = Callable[[str], Iterable[dict[str, Any]]]


def parse_link(link: str) -> str | None:
    link = link.strip()
    if re.fullmatch(ID, link):
        return link
    base, _, query = link.partition("?")
    for form in LINK_FORMS:
        match = form.fullmatch(base)
        if match:
            return match.group(1)
    if base == "https://drive.google.com/open":
        ids = urllib.parse.parse_qs(query).get("id", [])
        if ids and re.fullmatch(ID, ids[0]):
            return ids[0]
    return None


def category(mime_type: str) -> tuple[str, str]:
    if mime_type in CATEGORIES:
        return CATEGORIES[mime_type]
    if mime_type.startswith("image/"):
        return ("Images", "stored only")
    if mime_type.startswith("video/"):
        return ("Videos", "stored only")
    if mime_type.startswith(GOOGLE):
        return ("Other Google files", "skipped")
    return ("Other files", "stored only")


def walk(root_id: str, list_children: ListChildren) -> dict[str, Any]:
    """Walk all levels breadth-first; each folder once (FR-GDUI-0009)."""
    folders = [{"key": "f0", "depth": 0, "parent": None}]
    keys, items, revisited = {root_id: "f0"}, [], 0
    queue = collections.deque([(root_id, 0)])
    while queue:
        folder_id, depth = queue.popleft()
        for child in list_children(folder_id):
            if child["mimeType"] == FOLDER:
                if child["id"] in keys:
                    revisited += 1
                    continue
                keys[child["id"]] = f"f{len(folders)}"
                folders.append(
                    {
                        "key": keys[child["id"]],
                        "depth": depth + 1,
                        "parent": keys[folder_id],
                    }
                )
                queue.append((child["id"], depth + 1))
            else:
                items.append({**child, "folder": keys[folder_id]})
    return {"folders": folders, "items": items, "revisited": revisited}


def summary_lines(name: str, tree: dict[str, Any]) -> list[str]:
    """The test result text of FR-GDUI-0010, FR-GDUI-0011 and FR-GDUI-0013."""
    counts = collections.Counter(category(item["mimeType"]) for item in tree["items"])
    total, folders = sum(counts.values()), len(tree["folders"])
    lines = [
        name,
        f"We found {total} file{'' if total == 1 else 's'} in {folders} folder{'' if folders == 1 else 's'}.",
    ]
    for (label, kind), n in sorted(counts.items(), key=lambda kv: (CLASS_ORDER[kv[0][1]], -kv[1], kv[0][0])):
        lines.append(f"• {label}: {n}{FLAGS.get(kind, '')}")
    if any(kind != "processed" for _, kind in counts):
        lines.append("Only Google Docs, Slides, Sheets and PDFs become knowledge for now.")
    if not any(kind in ("processed", "stored only") for _, kind in counts):
        lines.append("There is nothing to import from this folder.")
    return lines


def recording(tree: dict[str, Any]) -> dict[str, Any]:
    """A recorded folder listing (DATA-GDUI-0003): real shape, generated names only."""
    numbers: collections.Counter[str] = collections.Counter()
    items = []
    for item in tree["items"]:
        label, _ = category(item["mimeType"])
        numbers[label] += 1
        items.append(
            {
                "name": f"{label} {numbers[label]}",
                "mimeType": item["mimeType"],
                "size": int(item["size"]) if "size" in item else None,
                "modifiedTime": item.get("modifiedTime"),
                "folder": item["folder"],
            }
        )
    folders = [
        {
            **f,
            "name": "Recorded folder" if f["depth"] == 0 else f"Folder {f['key'][1:]}",
        }
        for f in tree["folders"]
    ]
    return {
        "recorded": datetime.datetime.now().astimezone().date().isoformat(),
        "folders": folders,
        "items": items,
    }


def self_test() -> int:
    failures = 0
    for link, expected in LINK_CASES:
        accepted = parse_link(link) is not None
        failures += accepted != expected
        print(f"{'PASS' if accepted == expected else 'FAIL'}  link {link!r} accepted={accepted}")
    tree_data = {
        "root000000": [
            {"id": "sub1000000", "name": "Specs 2026", "mimeType": FOLDER},
            {
                "id": "doc1000000",
                "name": "Client brief",
                "mimeType": GOOGLE + "document",
            },
            {
                "id": "pdf1000000",
                "name": "Contract.pdf",
                "mimeType": "application/pdf",
                "size": "1200",
            },
        ],
        "sub1000000": [
            {"id": "root000000", "name": "loop back", "mimeType": FOLDER},
            {"id": "frm1000000", "name": "Survey", "mimeType": GOOGLE + "form"},
            {
                "id": "doc2000000",
                "name": "Notes.docx",
                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            },
        ],
    }
    tree = walk("root000000", lambda folder_id: tree_data.get(folder_id, []))
    expected_lines = [
        "Specs",
        "We found 4 files in 2 folders.",
        "• Google Docs: 1",
        "• PDF: 1",
        "• Word documents: 1 (stored, not read yet)",
        "• Other Google files: 1 (skipped)",
        "Only Google Docs, Slides, Sheets and PDFs become knowledge for now.",
    ]
    for got, want in zip(summary_lines("Specs", tree), expected_lines, strict=True):
        ok = got == want
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  summary {got!r}")
    ok = tree["revisited"] == 1
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  folder reached twice is counted once")
    text = json.dumps(recording(tree))
    leaked = [
        word
        for word in (
            "Client brief",
            "Contract",
            "Survey",
            "Notes",
            "Specs 2026",
            "root000000",
        )
        if word in text
    ]
    failures += bool(leaked)
    print(f"{'PASS' if not leaked else 'FAIL'}  recording has no real names or ids {leaked or ''}")
    print(f"{failures} failure(s)")
    return 1 if failures else 0


class DriveClient:
    """Authorised Drive v3 reads with retry on throttling; counts every HTTP call."""

    def __init__(self, session: AuthorizedSession) -> None:
        self.session = session
        self.calls = 0

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        attempt = 0
        while True:
            self.calls += 1
            response = self.session.get(
                f"{DRIVE}/{path}",
                params={"supportsAllDrives": "true", **params},
                timeout=60,
            )
            rate_limited = response.status_code == HTTPStatus.FORBIDDEN and "rateLimitExceeded" in response.text
            if response.status_code not in RETRY_STATUS and not rate_limited:
                break
            _BACKOFF.wait(2**attempt)
            attempt += 1
            if attempt == MAX_ATTEMPTS:
                break
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        result: dict[str, Any] = response.json()
        return result

    def list_children(self, parent_id: str, page_size: int) -> Iterable[dict[str, Any]]:
        token: str | None = None
        while True:
            paging: dict[str, str] = {"pageToken": token} if token else {}
            page = self.get(
                "files",
                q=f"'{parent_id}' in parents and trashed = false",
                pageSize=page_size,
                includeItemsFromAllDrives="true",
                **paging,
                fields="nextPageToken, files(id,name,mimeType,size,modifiedTime,shortcutDetails)",
            )
            yield from page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return


def print_report(
    root: dict[str, Any], tree: dict[str, Any], *, who: str, elapsed: float, calls: int, page_size: int
) -> None:
    print(f"Drive sees: {who}")
    print(f"folder in {'a shared drive' if root.get('driveId') else 'My Drive'}")
    depth = max(f["depth"] for f in tree["folders"])
    print(
        f"walk: {elapsed:.1f} s, {calls - 2} list calls, page size {page_size}, "
        f"max depth {depth}, folders reached twice {tree['revisited']}"
    )
    print("\nsummary as the modal would show it:")
    for line in summary_lines(root["name"], tree):
        print(f"  {line}")
    print("\nraw Drive types:")
    for mime_type, n in collections.Counter(item["mimeType"] for item in tree["items"]).most_common():
        print(f"  {n:5}  {mime_type}  -> {category(mime_type)[0]}")
    targets = collections.Counter(
        item.get("shortcutDetails", {}).get("targetMimeType", "?")
        for item in tree["items"]
        if item["mimeType"] == SHORTCUT
    )
    if targets:
        print(f"  shortcut targets: {dict(targets)}")
    sizes = sorted(int(item["size"]) for item in tree["items"] if "size" in item)
    if sizes:
        print(f"  uploaded files: {len(sizes)}, largest {sizes[-1] / 1e6:.1f} MB, total {sum(sizes) / 1e6:.1f} MB")


def print_error_probes(client: DriveClient, tree: dict[str, Any]) -> None:
    print("\nerror probes (FR-GDUI-0012):")
    a_file = next((item for item in tree["items"] if item["mimeType"] != SHORTCUT), None)
    if a_file:
        print(f"  a file id as the link -> {a_file['mimeType']} (the modal says: not a folder)")
    try:
        client.get(f"files/{UNKNOWN_ID}", fields="id")
        print("  unknown id -> found (unexpected)")
    except RuntimeError as error:
        print(f"  unknown id -> {str(error)[:8]} (the modal says: no access or not found)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("link", nargs="?", help="Drive folder link or id")
    parser.add_argument(
        "--record",
        action="store_true",
        help="print only a recorded folder listing (JSON)",
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--self-test", action="store_true", help="check link and category rules offline")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    folder_id = parse_link(args.link or "")
    if not folder_id:
        print("not an accepted Drive folder link (DATA-GDUI-0002)")
        return 2

    credentials, _ = google.auth.default(scopes=SCOPES)
    client = DriveClient(cast(Any, AuthorizedSession)(credentials))

    try:
        who = client.get("about", fields="user(emailAddress)")["user"]["emailAddress"]
        started = time.monotonic()
        root = client.get(f"files/{folder_id}", fields="id,name,mimeType,driveId")
        if root["mimeType"] != FOLDER:
            print(f"not a folder: {root['mimeType']}")
            return 1
        tree = walk(folder_id, lambda parent_id: client.list_children(parent_id, args.page_size))
        elapsed = time.monotonic() - started
    except RuntimeError as error:
        print(f"Drive error: {error}")
        return 1

    if args.record:
        print(json.dumps(recording(tree), indent=2))
        return 0

    print_report(root, tree, who=who, elapsed=elapsed, calls=client.calls, page_size=args.page_size)
    print_error_probes(client, tree)
    return 0


if __name__ == "__main__":
    sys.exit(main())
