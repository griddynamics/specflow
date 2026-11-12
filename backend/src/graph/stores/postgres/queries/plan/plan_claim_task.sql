-- Port of plan/queries/plan_claim_task.cypher. Lifecycle: claim one task. The
-- task is locked first (FOR UPDATE); then, in the same statement, a plan
-- persist in progress, the task's status, its area and story status and every
-- prerequisite's status are read, and the claim applies only when the task is
-- ready and its story is not held by a story claim. No row when the task does
-- not exist; status and task are those after the claim; holder and held_at are
-- the story claim's.
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
            WHEN EXISTS (
                SELECT 1
                FROM graph.node AS p
                WHERE
                    p.group_id = :project_id
                    AND p.kind = 'foreign'
                    AND p.labels[1] = 'Plan'
                    AND p.props ->> 'applying' IS NOT NULL
            ) THEN 'plan_applying'
            WHEN
                coalesce(locked.props ->> 'status', '') <> 'pending'
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
            WHEN NOT EXISTS (
                SELECT 1
                FROM graph.node AS a
                WHERE
                    a.group_id = :project_id
                    AND a.kind = 'foreign'
                    AND a.labels[1] = 'WorkArea'
                    AND a.props ->> 'area_id' = locked.props ->> 'area_id'
                    AND a.props ->> 'status' = 'active'
            ) THEN 'not_ready'
            WHEN NOT EXISTS (
                SELECT 1
                FROM graph.node AS s
                WHERE
                    s.group_id = :project_id
                    AND s.kind = 'foreign'
                    AND s.labels[1] = 'Story'
                    AND s.props ->> 'story_id' = locked.props ->> 'story_id'
                    AND s.props ->> 'status' = 'active'
            ) THEN 'not_ready'
            WHEN EXISTS (
                SELECT 1
                FROM graph.edge AS e
                INNER JOIN graph.node AS w
                    ON
                        e.target_id = w.id
                        AND w.kind = 'foreign'
                        AND w.labels[1] = 'Task'
                WHERE
                    e.source_id = locked.id
                    AND e.group_id = :project_id
                    AND e.rel_type = 'WAITS_FOR'
                    AND coalesce(w.props ->> 'status', '') <> 'completed'
            ) THEN 'not_ready'
            ELSE 'ok'
        END AS outcome
    FROM locked
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            jsonb_build_object(
                'status', 'in_progress',
                'claim_token', cast(:claim_token AS text),
                'claimed_by', cast(:claimed_by AS text),
                'claimed_at', cast(:now AS text),
                'attempts',
                coalesce(cast(node.props ->> 'attempts' AS integer), 0) + 1,
                'updated_at', cast(:now AS text)
            )
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
