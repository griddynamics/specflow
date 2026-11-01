-- Repair: restore the source fragment ids of edges addressed by id.
-- Parameters: relations (text[]), rows: a JSON list of id (text) and
-- fragment_ids (list). written counts the rows that found their edge.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'id' AS id,
        e.item -> 'fragment_ids' AS fragment_ids
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

updated AS (
    UPDATE graph.edge
    SET
        props
        = graph.props_merge(
            edge.props,
            jsonb_build_object('source_fragment_ids', k.fragment_ids)
        )
    FROM
        (SELECT DISTINCT ON (r1.id)
            r1.id,
            r1.fragment_ids
        FROM r AS r1
        ORDER BY r1.id ASC, r1.ord DESC) AS k
    WHERE
        cast(edge.id AS text) = k.id
        AND edge.group_id = :project_id
        AND edge.rel_type = any(cast(:relations AS text[]))
    RETURNING edge.id
)

SELECT count(*) AS written
FROM r
INNER JOIN graph.edge AS x
    ON
        cast(x.id AS text) = r.id
        AND x.group_id = :project_id
        AND x.rel_type = any(cast(:relations AS text[]))
