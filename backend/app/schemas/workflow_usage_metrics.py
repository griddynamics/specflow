"""Nested workflow → workspace → model usage stored on generation documents."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.schemas.model_token_usage import FLAT_AGGREGATE_MODEL_NAME, ModelTokenUsage, aggregate_flat_model_usage_from_tree

# Firestore field: workflow name/phase string → workspaces → models → ModelTokenUsage dict
WORKFLOW_USAGE_METRICS_FIELD = "workflow_usage_metrics"


def _matches_workflow(workflow_key: str, workflow_prefix: Optional[str]) -> bool:
    """True when no prefix is given, or ``workflow_key`` equals/starts with it.

    Workflow keys follow ``TelemetryWorkflowLabel.to_stored_string``: plain
    labels (e.g. ``kb_init``) and phased keys ``{kind}_phase_{n}_{role}``. A
    prefix with a trailing underscore (e.g. ``generation_phase_1_``) selects
    every role of one phase without also matching ``generation_phase_10_*``.
    """
    if not workflow_prefix:
        return True
    return workflow_key == workflow_prefix or workflow_key.startswith(workflow_prefix)


def aggregate_flat_model_usage(tree: Dict[str, Any] | None) -> ModelTokenUsage:
    """Sum all model rows in ``workflow_usage_metrics`` into one aggregate ``ModelTokenUsage``."""
    if not tree:
        return ModelTokenUsage(model_name=FLAT_AGGREGATE_MODEL_NAME)
    return aggregate_flat_model_usage_from_tree(tree)


def aggregate_model_usage_by_workspace(
    tree: Dict[str, Any] | None,
    workflow_prefix: Optional[str] = None,
) -> Dict[str, ModelTokenUsage]:
    """Sum per workspace id across workflows (for completion notifications / P10Y extras).

    With ``workflow_prefix`` set, only workflows whose key equals/starts with the
    prefix are summed — scopes a workspace's usage to the currently active step
    (e.g. ``kb_init`` or ``generation_phase_2_``) rather than its lifetime total.
    Default (None) sums every workflow, preserving existing caller behaviour.
    """
    out: Dict[str, ModelTokenUsage] = {}
    if not tree:
        return out
    for wf_key, wf_data in tree.items():
        if not isinstance(wf_data, dict) or not _matches_workflow(wf_key, workflow_prefix):
            continue
        for ws_id, ws_data in wf_data.get("workspaces", {}).items():
            if not isinstance(ws_data, dict):
                continue
            models = ws_data.get("models") or {}
            if not isinstance(models, dict):
                continue
            acc = out.setdefault(
                ws_id, ModelTokenUsage(model_name=FLAT_AGGREGATE_MODEL_NAME)
            )
            for row in models.values():
                if not isinstance(row, dict):
                    continue
                mtu = ModelTokenUsage.from_dict(row)
                acc = acc + mtu
            out[ws_id] = acc
    return out


def model_names_by_workspace(
    tree: Dict[str, Any] | None,
    workflow_prefix: Optional[str] = None,
) -> Dict[str, list[str]]:
    """Distinct model names seen per workspace id across workflows.

    Used by the status endpoint to surface which model(s) a workspace ran on in
    the TUI stats panel. Skips the empty aggregate model name and de-duplicates
    while preserving first-seen order. With ``workflow_prefix`` set, only
    workflows matching the prefix contribute — scopes the model list to the
    currently active step instead of every workflow ever run on the workspace.
    """
    out: Dict[str, list[str]] = {}
    if not tree:
        return out
    for wf_key, wf_data in tree.items():
        if not isinstance(wf_data, dict) or not _matches_workflow(wf_key, workflow_prefix):
            continue
        for ws_id, ws_data in wf_data.get("workspaces", {}).items():
            if not isinstance(ws_data, dict):
                continue
            models = ws_data.get("models") or {}
            if not isinstance(models, dict):
                continue
            names = out.setdefault(ws_id, [])
            for mname in models:
                if mname and mname != FLAT_AGGREGATE_MODEL_NAME and mname not in names:
                    names.append(mname)
    return out


def merge_token_usage_into_tree(
    tree: Dict[str, Any],
    workflow_key: str,
    workspace_key: str,
    delta: ModelTokenUsage,
) -> Dict[str, Any]:
    """
    Return a **new** tree with ``delta`` merged into
    ``tree[workflow_key].workspaces[workspace_key].models[delta.model_name]``.
    """
    if delta.is_empty():
        return tree

    # Path-copy: only shallow-copy the 3 dicts along the mutation path (O(depth=3))
    root = dict(tree) if tree else {}
    wf = dict(root.get(workflow_key, {}))
    ws_map = dict(wf.get("workspaces", {}))
    ws_bucket = dict(ws_map.get(workspace_key, {}))
    models = dict(ws_bucket.get("models", {}))

    mkey = delta.model_name
    if mkey in models:
        existing = ModelTokenUsage.from_dict(models[mkey])
        # Use + rather than merged_with: handles any stored key/model_name mismatch
        # (data corruption) without raising, preserving accumulation for non-essential metrics.
        models[mkey] = (existing + delta).to_dict()
    else:
        models[mkey] = delta.to_dict()

    ws_bucket["models"] = models
    ws_map[workspace_key] = ws_bucket
    wf["workspaces"] = ws_map
    root[workflow_key] = wf
    return root
