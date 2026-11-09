-- Port of plan/queries/plan_upsert_story_implements.cypher. Persist: MERGE
-- IMPLEMENTS from a story to its anchor spec node (by uuid). Rows: a JSON
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
            AND s.labels[1] = 'Story'
            AND s.props ->> 'story_id' = r.source
    INNER JOIN graph.node AS t
        ON
            t.group_id = :project_id
            AND t.kind = 'spec'
            AND r.target = t.uuid
),

updated AS (
    UPDATE graph.edge
    SET props = graph.props_merge(edge.props, ends.patch)
    FROM ends
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = 'IMPLEMENTS'
        AND edge.edge_key = ends.edge_key
        AND edge.source_id = ends.source_id
        AND edge.target_id = ends.target_id
    RETURNING edge.id
),

inserted AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'IMPLEMENTS' AS rel_type,
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
                AND x.rel_type = 'IMPLEMENTS'
                AND x.edge_key = ends.edge_key
                AND x.source_id = ends.source_id
                AND x.target_id = ends.target_id
        )
    RETURNING edge.id
)

SELECT count(*) AS written FROM ends
