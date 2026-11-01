-- Delete a node without domain label by its key (its edges cascade).
-- __NODE__ is a slot for the node match rendered from the match_*_node fragments by the dialect;
-- its default is the match of a Document by :key.
WITH deleted AS (
    DELETE FROM graph.node AS n
    WHERE
        n.id IN (
            /*__NODE__*/SELECT m.id FROM graph.node AS m
            WHERE m.group_id = :project_id AND m.kind = 'document' AND m.document_id = :key/*__NODE__*/
        )
    RETURNING n.id
)

SELECT count(*) AS deleted FROM deleted
