-- Persist: MERGE edges of one relation by row.edge_key between the nodes with
-- uuids row.source and row.target. Parameters: rel_type, rows: a JSON list of
-- source, target, edge_key and props. Rows of one edge are folded in order. An
-- edge that exists is updated, a missing one inserted. written counts the rows
-- whose two nodes exist.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'source' AS source,
        e.item ->> 'target' AS target,
        e.item ->> 'edge_key' AS edge_key,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

ends AS (
    SELECT
        r.edge_key,
        r.props,
        r.ord,
        s.id AS source_id,
        t.id AS target_id
    FROM r
    INNER JOIN graph.node AS s
        ON s.group_id = :project_id AND s.kind = 'spec' AND r.source = s.uuid
    INNER JOIN graph.node AS t
        ON t.group_id = :project_id AND t.kind = 'spec' AND r.target = t.uuid
),

folded AS (
    SELECT
        x.edge_key,
        x.source_id,
        x.target_id,
        graph.props_fold(array_agg(x.props ORDER BY x.ord)) AS patch
    FROM ends AS x
    GROUP BY x.edge_key, x.source_id, x.target_id
),

updated AS (
    UPDATE graph.edge
    SET props = graph.props_merge(edge.props, f.patch)
    FROM folded AS f
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = :rel_type
        AND edge.edge_key = f.edge_key
        AND edge.source_id = f.source_id
        AND edge.target_id = f.target_id
    RETURNING edge.id
),

inserted AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        cast(:rel_type AS text) AS rel_type,
        f.source_id,
        f.target_id,
        graph.props_merge(jsonb_build_object('edge_key', f.edge_key), f.patch)
            AS props
    FROM folded AS f
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.edge AS x
            WHERE
                x.group_id = :project_id
                AND x.rel_type = :rel_type
                AND x.edge_key = f.edge_key
                AND x.source_id = f.source_id
                AND x.target_id = f.target_id
        )
    ON CONFLICT (group_id, rel_type, edge_key, source_id, target_id) DO NOTHING
    RETURNING edge.id
)

SELECT count(*) AS written FROM ends
