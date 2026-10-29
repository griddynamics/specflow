"""Fixtures for pipeline.yml, paired with it by stem for `semgrep --test` (which ignores
`paths:`)."""

import os
import shutil
import uuid
from pathlib import Path
from uuid import uuid4

# --- kit.configurable.no-random-id-in-pipeline-output ---


def new_batch_id() -> str:
    # ruleid: kit.configurable.no-random-id-in-pipeline-output
    return str(uuid.uuid4())


def new_item_id() -> str:
    # ruleid: kit.configurable.no-random-id-in-pipeline-output
    return str(uuid4())


def item_id(batch_checksum: str, index: int) -> str:
    # ok: kit.configurable.no-random-id-in-pipeline-output
    return f"{batch_checksum}:{index}"


def stable_item_id(batch_checksum: str, index: int) -> str:
    # ok: kit.configurable.no-random-id-in-pipeline-output
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{batch_checksum}/{index}"))


# --- kit.configurable.no-delete-of-immutable-file ---


def clear_original(original_path: str) -> None:
    # ruleid: kit.configurable.no-delete-of-immutable-file
    os.remove(original_path)


def clear_original_tree(original_dir: str) -> None:
    # ruleid: kit.configurable.no-delete-of-immutable-file
    shutil.rmtree(original_dir)


def unlink_original(original_file: Path) -> None:
    # ruleid: kit.configurable.no-delete-of-immutable-file
    original_file.unlink(missing_ok=True)


def clear_converted_cache(converted_path: str) -> None:
    # ok: kit.configurable.no-delete-of-immutable-file
    os.remove(converted_path)


def clear_render_cache(render_dir: Path) -> None:
    # ok: kit.configurable.no-delete-of-immutable-file
    shutil.rmtree(render_dir)
