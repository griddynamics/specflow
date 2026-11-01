"""The frontend's upload table equals the API's.

``api/fixtures/upload-formats.json`` is read by the frontend test
``frontend/tests/production/upload-formats.test.ts``; this test keeps it equal to
``file_parsers.formats`` (extension, canonical type, refused legacy) and the caps of ``api.uploads``.
"""

from __future__ import annotations

import json
from pathlib import Path

from api import uploads
from file_parsers.formats import FORMATS, LEGACY_FORMATS

CONTRACT = Path(uploads.__file__).resolve().parent / "fixtures" / "upload-formats.json"


def test_the_upload_contract_json_equals_the_python_table() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert contract["formats"] == [
        {"extension": f.extension, "media_type": f.media_type, "max_bytes": uploads.MAX_BYTES[f.format]}
        for f in FORMATS
    ]
    assert contract["legacy_extensions"] == [legacy.extension for legacy in LEGACY_FORMATS]
