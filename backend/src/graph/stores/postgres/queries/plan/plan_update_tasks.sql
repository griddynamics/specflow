-- Port of plan/queries/plan_update_tasks.cypher. Persist: update tasks only
-- while their status is still the one the run read. The task is matched on
-- its immutable key and locked first (FOR UPDATE), so a claim committed
-- meanwhile is seen and the task is skipped into the run's conflicts; a task
-- this run already updated (a resumed batch) applies again. Rows: a JSON list
-- of id, expected_status and properties. One row: the ids of the tasks that
-- did not apply (the run's conflicts).
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'id' AS id,
        e.item ->> 'expected_status' AS expected_status,
        coalesce(e.item -> 'properties', cast('{}' AS jsonb)) AS properties
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

locked AS (
    SELECT
        n.id,
        n.props
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'foreign'
        AND n.labels[1] = 'Task'
        AND n.props ->> 'task_id' IN (SELECT r.id FROM r)
    FOR UPDATE
),

decided AS (
    SELECT
        locked.id,
        r.ord,
        r.id AS task_id,
        r.properties,
        coalesce(
            locked.props ->> 'status' = r.expected_status
            OR locked.props ->> 'last_run_id' = :run_id,
            FALSE
        ) AS applies
    FROM r
    INNER JOIN locked ON locked.props ->> 'task_id' = r.id
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            decided.properties
            || jsonb_build_object(
                'updated_at', cast(:now AS text),
                'last_run_id', cast(:run_id AS text)
            )
        )
    FROM decided
    WHERE node.id = decided.id AND decided.applies
    RETURNING node.id
)

SELECT
    coalesce(
        array_agg(decided.task_id ORDER BY decided.ord) FILTER (
            WHERE NOT decided.applies
        ),
        cast(ARRAY[] AS text[])
    ) AS conflicts
FROM decided
