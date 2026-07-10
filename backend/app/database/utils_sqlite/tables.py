"""Physical layout registry: every collection is a real table, registered here.

Everything is a table (there is no "subcollection" — that is a Firestore word for what
SQL calls a table with a compound primary key).

``columns`` promotes the stable scalar core of each document to real, typed SQL
columns — every field that is a plain scalar (str/int/float/bool/datetime) written
by the app, so the table is genuinely inspectable in a SQL browser. Nested/open-ended
structures (arrays, dicts, per-workflow maps) stay in the JSON `data` blob, which
remains the source of truth on every read; promoted columns are write-through
mirrors. ``indexes`` lists only the column combinations something actually
filters/orders on — promotion for inspectability and indexing for query performance
are separate concerns.
"""

from __future__ import annotations

from typing import Dict

from app.database.utils_sqlite.schemas import DOC_ID, _Table

_TABLES: tuple[_Table, ...] = (
    _Table(
        "generation_sessions",
        (DOC_ID,),
        {
            # Queried/ordered (see indexes below)
            "status": "TEXT",
            "status_changed_at": "TEXT",
            "last_activity_at": "TEXT",
            "shutdown_interrupted": "INTEGER",
            "key_uid": "TEXT",
            "created_at": "TEXT",
            # Rest of the stable scalar core (not queried, promoted for inspectability)
            "checkpoint": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "failed_at": "TEXT",
            "error": "TEXT",
            "retry_count": "INTEGER",
            "max_retries": "INTEGER",
            "user_email": "TEXT",
            "workspace_pool": "TEXT",
            "specification_dir": "TEXT",
            "outputs_archived": "INTEGER",
            "code_archived": "INTEGER",
            "artifact_path": "TEXT",
            "emergency_archived": "INTEGER",
            "total_usd_cost": "REAL",
        },
        (
            ("status", "last_activity_at"),
            ("status", "status_changed_at"),
            ("status", "shutdown_interrupted"),
            ("key_uid", "created_at"),
        ),
    ),
    _Table(
        "workspaces",
        (DOC_ID,),
        {
            # Queried/ordered (see indexes below)
            "status": "TEXT",
            "workspace_pool": "TEXT",
            "set_number": "INTEGER",
            "scheduled_for_wipe": "INTEGER",
            "scheduled_for_wipe_at": "TEXT",
            "locked_by": "TEXT",
            "clean_verified": "INTEGER",
            # Rest of the stable scalar core (not queried, promoted for inspectability)
            "repo_url": "TEXT",
            "p10y_repository_id": "INTEGER",
            "locked_at": "TEXT",
            "lease_expires_at": "TEXT",
            "cleaning_started_at": "TEXT",
            "last_used_by": "TEXT",
            "last_cleaned_at": "TEXT",
            "error": "TEXT",
            "stuck_reason": "TEXT",
            "stuck_at": "TEXT",
            "force_released": "INTEGER",
            "force_release_reason": "TEXT",
            "force_released_by": "TEXT",
            "force_released_at": "TEXT",
        },
        (
            ("status",),
            ("workspace_pool", "set_number"),
            ("scheduled_for_wipe", "scheduled_for_wipe_at"),
        ),
    ),
    _Table(
        "api_keys",
        (DOC_ID,),
        {
            # Queried/ordered (see indexes below)
            "key_uid": "TEXT",
            # Rest of the stable scalar core (not queried, promoted for inspectability)
            "workspace_pool": "TEXT",
            "user_id": "TEXT",
            "user_name": "TEXT",
            "created_at": "TEXT",
            "last_used_at": "TEXT",
            "expires_at": "TEXT",
            "is_active": "INTEGER",
            "github_token_ciphertext": "TEXT",
            "github_token_set_at": "TEXT",
            "git_user_name": "TEXT",
            "max_concurrent_sessions": "INTEGER",
        },
        (("key_uid",),),
    ),
    _Table(
        "workspace_model_usage",
        ("generation_id", "workspace_id"),
        parent="generation_sessions",
    ),
)

_TABLE: Dict[str, _Table] = {t.name: t for t in _TABLES}
