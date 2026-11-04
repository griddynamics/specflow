-- Persist: delete the edge row.uuid stored under row.edge_key (an edge without
-- a uuid: by key). Parameters: rel_type, rows: a JSON list of edge_key and
-- uuid. written counts the deleted edges.
WITH r AS (
    SELECT
        e.item ->> 'edge_key' AS edge_key,
        e.item ->> 'uuid' AS uuid
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

deleted AS (
    DELETE FROM graph.edge
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = :rel_type
        AND edge.id IN (
            SELECT x.id
            FROM r
            INNER JOIN graph.edge AS x
                ON
                    x.group_id = :project_id
                    AND x.rel_type = :rel_type
                    AND r.edge_key = x.edge_key
                    AND (
                        x.props ->> 'uuid' = r.uuid
                        OR x.props ->> 'uuid' IS NULL
                    )
        )
    RETURNING edge.id
)

SELECT count(*) AS written FROM deleted
