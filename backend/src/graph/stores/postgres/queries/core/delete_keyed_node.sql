-- Delete a node without domain label by its key; its edges cascade.
-- NODE is a slot for the node match the dialect renders from the
-- match_*_node fragments; its default matches a Document by :key.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.id IN (
            /*__NODE__*/SELECT m.id FROM graph.node AS m
            WHERE
                m.group_id = :project_id
                AND m.kind = 'document'
                AND m.document_id = :key/*__NODE__*/
        )
    RETURNING node.id
)

SELECT count(*) AS deleted FROM deleted
