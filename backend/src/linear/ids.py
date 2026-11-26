"""Deterministic client ids: Linear accepts a client UUID ``id`` on create, so a retried create answers "already
exists" instead of making a duplicate. The id of an object is a UUIDv5 of the Graphico project, the kind of the
object and its node id."""

from __future__ import annotations

import uuid
from typing import Final

# A fixed namespace of the Graphico exports to Linear; never change it (every id made so far derives from it).
NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "https://graphico.app/integrations/linear")

KIND_PROJECT: Final = "project"
KIND_MILESTONE: Final = "milestone"
KIND_LABEL: Final = "label"
KIND_STORY: Final = "story"
KIND_TASK: Final = "task"
KIND_RELATION: Final = "relation"
KIND_PARENT: Final = "parent"  # the "Open questions" issue of the gap channel
KIND_GAP: Final = "gap"
KIND_COMMENT: Final = "comment"


def node_uuid(project_id: uuid.UUID | str, kind: str, node_id: str) -> uuid.UUID:
    """The client id of the Linear object of ``kind`` made for plan node ``node_id`` of Graphico ``project_id``."""

    if not kind.strip() or not node_id.strip():
        raise ValueError("kind and node_id must not be blank")
    return uuid.uuid5(NAMESPACE, f"{uuid.UUID(str(project_id))}:{kind}:{node_id}")
