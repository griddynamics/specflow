-- The relationship types of one node without domain label.
-- NODE is a slot for the node match the dialect renders from the
-- match_*_node fragments; its default matches a Document by :n_key.
WITH node AS (
    /*__NODE__*/SELECT m.id FROM graph.node AS m
    WHERE
        m.group_id = :project_id
        AND m.kind = 'document'
        AND m.document_id = :n_key/*__NODE__*/
)

SELECT
    coalesce(
        array_agg(DISTINCT e.rel_type ORDER BY e.rel_type)
        FILTER (WHERE e.rel_type IS NOT NULL),
        cast(ARRAY[] AS text[])
    ) AS types
FROM node
LEFT JOIN graph.edge AS e ON node.id = e.source_id OR node.id = e.target_id
