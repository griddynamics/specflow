-- The relationship types of one node without domain label.
-- __NODE__ is a slot for the node match rendered from the match_*_node fragments by the dialect;
-- its default is the match of a Document by :n_key.
WITH node AS (
    /*__NODE__*/SELECT m.id FROM graph.node AS m
    WHERE m.group_id = :project_id AND m.kind = 'document' AND m.document_id = :n_key/*__NODE__*/
)

SELECT coalesce(
    array_agg(DISTINCT e.type ORDER BY e.type) FILTER (WHERE e.type IS NOT NULL),
    CAST(ARRAY[] AS text [])
) AS types
FROM node
LEFT JOIN graph.edge AS e ON node.id = e.source_id OR node.id = e.target_id
