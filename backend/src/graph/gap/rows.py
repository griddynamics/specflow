"""Stored findings as models, their keys, and the sort and page order of ``findings``."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from graph.core.errors import ConfigurationError
from graph.core.identity import NAMESPACE
from graph.gap.models import Finding, FindingRef
from graph.gap.names import FINDING_PREFIX

Position = tuple[int, int, str]
POSITION_PARTS = 3


def new_finding_id() -> str:
    """``FND::`` and 12 random upper-case hex digits."""

    return FINDING_PREFIX + secrets.token_hex(6).upper()


def finding_uuid(project_id: str, finding_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{project_id}|gap|{finding_id}"))


def ref_uuid(project_id: str, ref: FindingRef) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{project_id}|gap|ref|{ref.type}|{ref.value}"))


def about_edge_key(finding_id: str, spec_id: str) -> str:
    return f"{finding_id}|ABOUT|{spec_id}"


def refers_edge_key(finding_id: str, ref_node_uuid: str) -> str:
    return f"{finding_id}|REFERS_TO|{ref_node_uuid}"


def to_finding(row: Mapping[str, Any]) -> Finding:
    """The model of one ``gap_read_findings`` row."""

    props: Mapping[str, Any] = row["finding"]
    refs = sorted((tuple(pair) for pair in row["refs"] or ()), key=lambda pair: (pair[0], pair[1]))
    return Finding(
        finding_id=props["finding_id"],
        kind=props["kind"],
        text=props["text"],
        origin=props.get("origin", "agent"),
        status=props["status"],
        blocking=bool(props.get("blocking", False)),
        about_spec_ids=tuple(props.get("about_spec_ids") or ()),
        story_id=props.get("story_id"),
        created_by=props["created_by"],
        created_at=props["created_at"],
        resolved_by=props.get("resolved_by"),
        resolved_at=props.get("resolved_at"),
        resolution_note=props.get("resolution_note"),
        orphaned=bool(props.get("orphaned", False)),
        updated_at=props.get("updated_at"),
        refers_to=tuple(FindingRef(type=kind, value=value) for kind, value in refs),
    )


def position(finding: Finding) -> Position:
    """Sort key: blocking first, then newest first, then ``finding_id``."""

    micros = int(finding.created_at.timestamp() * 1_000_000)
    return (0 if finding.blocking else 1, -micros, finding.finding_id)


def sorted_findings(findings: Sequence[Finding]) -> list[Finding]:
    return sorted(findings, key=position)


def encode_after(key: Position) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def decode_after(token: str) -> Position:
    """The position an ``after`` token names, or ``ConfigurationError("invalid_limit")``."""

    try:
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if isinstance(raw, list) and len(raw) == POSITION_PARTS and isinstance(raw[2], str):
            return int(raw[0]), int(raw[1]), raw[2]
    except (binascii.Error, ValueError, TypeError):
        pass
    raise ConfigurationError("invalid_limit")
