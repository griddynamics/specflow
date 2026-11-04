-- Repair: restore one REFERENCED_BY per evidence line range, to fragments that
-- exist. Parameter rows: a JSON list of id (text of the node id) and evidence
-- (fragment_id, line_start, line_end). A relationship with those lines already
-- there is kept, not repeated. written counts the rows that found their node.
WITH r AS (
    SELECT
        n.id,
        coalesce(e.item -> 'evidence', cast('[]' AS jsonb)) AS evidence
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
    INNER JOIN graph.node AS n
        ON
            cast(n.id AS text) = e.item ->> 'id'
            AND n.group_id = :project_id
            AND n.kind = 'spec'
),

wanted AS (
    SELECT DISTINCT
        r.id AS node_id,
        f.id AS fragment_node,
        ev.item -> 'line_start' AS line_start,
        ev.item -> 'line_end' AS line_end
    FROM r
    CROSS JOIN LATERAL jsonb_array_elements(r.evidence) AS ev (item)
    INNER JOIN graph.node AS f
        ON
            f.group_id = :project_id
            AND f.kind = 'fragment'
            AND f.fragment_id = ev.item ->> 'fragment_id'
),

created AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'REFERENCED_BY' AS rel_type,
        w.node_id AS source_id,
        w.fragment_node AS target_id,
        graph.props_merge(
            cast('{}' AS jsonb),
            jsonb_build_object(
                'line_start', w.line_start, 'line_end', w.line_end
            )
        )
            AS props
    FROM wanted AS w
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.edge AS x
            WHERE
                x.rel_type = 'REFERENCED_BY'
                AND x.source_id = w.node_id
                AND x.target_id = w.fragment_node
                AND x.group_id = :project_id
                AND x.props -> 'line_start' IS NOT DISTINCT FROM w.line_start
                AND x.props -> 'line_end' IS NOT DISTINCT FROM w.line_end
        )
    RETURNING edge.id
)

SELECT count(*) AS written FROM r
