-- Port of plan/queries/plan_create_tasks.cypher. Persist: create new tasks by
-- task id; a task this run created already (a resumed batch) is left as it
-- is. Rows: a JSON list of id, uuid and properties. The uuid is derived from
-- the task id, so the foreign node key decides whether the task exists.
WITH r AS (
    SELECT
        e.item ->> 'id' AS id,
        e.item ->> 'uuid' AS uuid,
        coalesce(e.item -> 'properties', cast('{}' AS jsonb)) AS properties
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

inserted AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    SELECT
        :project_id AS group_id,
        'foreign' AS kind,
        ARRAY['Task'] AS labels,
        graph.props_merge(
            jsonb_build_object('task_id', r.id),
            r.properties
            || jsonb_build_object(
                'uuid', r.uuid,
                'attempts', 0,
                'created_at', cast(:now AS text),
                'updated_at', cast(:now AS text),
                'created_run_id', cast(:run_id AS text),
                'last_run_id', cast(:run_id AS text)
            )
        ) AS props
    FROM r
    ON CONFLICT (group_id, (labels[1]), uuid) WHERE kind = 'foreign'
    DO NOTHING
    RETURNING node.id
)

SELECT count(*) AS written FROM r
