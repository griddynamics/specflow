-- Port of gap/queries/gap_delete_unused_refs.cypher. Delete the ExternalRef
-- nodes of :uuids that no finding refers to any more.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'ExternalRef'
        AND node.uuid = any(cast(:uuids AS text[]))
        AND NOT EXISTS (
            SELECT 1
            FROM graph.edge AS e
            WHERE
                e.group_id = :project_id
                AND e.rel_type = 'REFERS_TO'
                AND e.target_id = node.id
        )
    RETURNING node.id
)

SELECT count(*) AS deleted FROM deleted
