-- Persist: SET += row.props on the edge row.uuid stored under one of row.keys
-- (an edge without a uuid is matched on its key alone). Parameters: rel_type,
-- rows: a JSON list of uuid, keys and props. written counts the edges that
-- matched.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'uuid' AS uuid,
        e.item -> 'keys' AS keys,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

matched AS (
    SELECT DISTINCT ON (x.id)
        x.id,
        r.props
    FROM r
    INNER JOIN graph.edge AS x
        ON
            x.group_id = :project_id
            AND x.rel_type = :rel_type
            AND jsonb_exists(r.keys, x.edge_key)
            AND (x.props ->> 'uuid' = r.uuid OR x.props ->> 'uuid' IS NULL)
    ORDER BY x.id ASC, r.ord DESC
),

updated AS (
    UPDATE graph.edge
    SET props = graph.props_merge(edge.props, m.props)
    FROM matched AS m
    WHERE edge.id = m.id
    RETURNING edge.id
)

SELECT count(*) AS written FROM matched
