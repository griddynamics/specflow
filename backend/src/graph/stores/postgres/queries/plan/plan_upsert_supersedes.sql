-- Port of plan/queries/plan_upsert_supersedes.cypher. Persist: MERGE
-- SUPERSEDES from a new task revision to the task it replaces. Rows: a JSON
-- list of source, target and key. written counts the rows whose two nodes
-- exist.
WITH r AS (
    SELECT
        cast('{}' AS jsonb) AS patch,
        e.item ->> 'source' AS source,
        e.item ->> 'target' AS target,
        e.item ->> 'key' AS edge_key
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

ends AS (
    SELECT
        r.edge_key,
        r.patch,
        s.id AS source_id,
        t.id AS target_id
    FROM r
    INNER JOIN graph.node AS s
        ON
            s.group_id = :project_id
            AND s.kind = 'foreign'
            AND s.labels[1] = 'Task'
            AND s.props ->> 'task_id' = r.source
    INNER JOIN graph.node AS t
        ON
            t.group_id = :project_id
            AND t.kind = 'foreign'
            AND t.labels[1] = 'Task'
            AND t.props ->> 'task_id' = r.target
),

updated AS (
    UPDATE graph.edge
    SET props = graph.props_merge(edge.props, ends.patch)
    FROM ends
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = 'SUPERSEDES'
        AND edge.edge_key = ends.edge_key
        AND edge.source_id = ends.source_id
        AND edge.target_id = ends.target_id
    RETURNING edge.id
),

inserted AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'SUPERSEDES' AS rel_type,
        ends.source_id,
        ends.target_id,
        graph.props_merge(
            jsonb_build_object('edge_key', ends.edge_key), ends.patch
        ) AS props
    FROM ends
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.edge AS x
            WHERE
                x.group_id = :project_id
                AND x.rel_type = 'SUPERSEDES'
                AND x.edge_key = ends.edge_key
                AND x.source_id = ends.source_id
                AND x.target_id = ends.target_id
        )
    RETURNING edge.id
)

SELECT count(*) AS written FROM ends
