-- Port of plan/queries/plan_transition_task.cypher. Lifecycle: one task state
-- change. The task is matched on its immutable key and locked first (FOR
-- UPDATE), so its status and claim token are read after any concurrent change
-- commits; the change applies only from an allowed status, when its story is
-- not held by a story claim and, when asked, with the matching claim token. No
-- row when the task does not exist; status and task are those after the change;
-- holder and held_at are the story claim's.
WITH locked AS (
    SELECT
        n.id,
        n.props
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'foreign'
        AND n.labels[1] = 'Task'
        AND n.props ->> 'task_id' = :task_id
    FOR UPDATE
),

decided AS (
    SELECT
        locked.id,
        locked.props,
        CASE
            WHEN
                NOT coalesce(
                    locked.props ->> 'status'
                    = any(cast(:from_statuses AS text[])),
                    FALSE
                )
                THEN 'wrong_state'
            WHEN EXISTS (
                SELECT 1
                FROM graph.node AS s
                WHERE
                    s.group_id = :project_id
                    AND s.kind = 'foreign'
                    AND s.labels[1] = 'Story'
                    AND s.props ->> 'story_id' = locked.props ->> 'story_id'
                    AND s.props ->> 'claimed_by' IS NOT NULL
            ) THEN 'story_claimed'
            WHEN
                cast(:check_token AS boolean)
                AND coalesce(locked.props ->> 'claim_token', '') <> :claim_token
                THEN 'claim_mismatch'
            ELSE 'ok'
        END AS outcome
    FROM locked
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            cast(:properties AS jsonb)
            || jsonb_build_object('updated_at', cast(:now AS text))
        )
    FROM decided
    WHERE node.id = decided.id AND decided.outcome = 'ok'
    RETURNING node.id, node.props
)

SELECT
    decided.outcome,
    coalesce(updated.props, decided.props) ->> 'status' AS status,
    coalesce(updated.props, decided.props) AS task,
    (
        SELECT s.props ->> 'claimed_by'
        FROM graph.node AS s
        WHERE
            s.group_id = :project_id
            AND s.kind = 'foreign'
            AND s.labels[1] = 'Story'
            AND s.props ->> 'story_id' = decided.props ->> 'story_id'
    ) AS holder,
    (
        SELECT s.props ->> 'claimed_at'
        FROM graph.node AS s
        WHERE
            s.group_id = :project_id
            AND s.kind = 'foreign'
            AND s.labels[1] = 'Story'
            AND s.props ->> 'story_id' = decided.props ->> 'story_id'
    ) AS held_at
FROM decided
LEFT JOIN updated ON decided.id = updated.id
