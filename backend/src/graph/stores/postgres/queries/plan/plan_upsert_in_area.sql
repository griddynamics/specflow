-- Port of plan/queries/plan_upsert_in_area.cypher. Persist: MERGE IN_AREA from
-- a spec node (by uuid) to its work area (by area id), with the placement
-- rule and edges in row.properties (a null removes a property). Rows: a JSON
-- list of source, target, key and properties. written counts the rows whose
-- two nodes exist.
WITH r AS (
    SELECT
        e.item ->> 'source' AS source,
        e.item ->> 'target' AS target,
        e.item ->> 'key' AS edge_key,
        coalesce(e.item -> 'properties', cast('{}' AS jsonb)) AS patch
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
            AND s.kind = 'spec'
            AND r.source = s.uuid
    INNER JOIN graph.node AS t
        ON
            t.group_id = :project_id
            AND t.kind = 'foreign'
            AND t.labels[1] = 'WorkArea'
            AND t.props ->> 'area_id' = r.target
),

updated AS (
    UPDATE graph.edge
    SET props = graph.props_merge(edge.props, ends.patch)
    FROM ends
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = 'IN_AREA'
        AND edge.edge_key = ends.edge_key
        AND edge.source_id = ends.source_id
        AND edge.target_id = ends.target_id
    RETURNING edge.id
),

inserted AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'IN_AREA' AS rel_type,
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
                AND x.rel_type = 'IN_AREA'
                AND x.edge_key = ends.edge_key
                AND x.source_id = ends.source_id
                AND x.target_id = ends.target_id
        )
    RETURNING edge.id
)

SELECT count(*) AS written FROM ends
