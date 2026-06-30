#!/usr/bin/env python3
"""Print TruffleHog JSON findings without secret values."""

from __future__ import annotations

import json
import sys
from typing import Any


def _source_path(source_metadata: dict[str, Any]) -> str:
    data = source_metadata.get("Data")
    if not isinstance(data, dict):
        return "<unknown>"

    for value in data.values():
        if not isinstance(value, dict):
            continue
        for key in ("file", "path", "commit", "repository", "link"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return "<unknown>"


def main() -> int:
    findings = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        findings += 1
        detector = payload.get("DetectorName") or payload.get("DetectorType") or "<unknown>"
        verified = payload.get("Verified", False)
        source = _source_path(payload.get("SourceMetadata") or {})
        print(f"trufflehog finding: detector={detector} verified={verified} source={source}")

    if findings:
        print(f"trufflehog findings: {findings}")
        return 1

    print("trufflehog findings: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
