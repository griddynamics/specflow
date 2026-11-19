-- Port of gap/queries/gap_delete_first_slice_findings.cypher. Delete the
-- first-slice Finding nodes of the project; their edges cascade.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'Finding'
    RETURNING node.id
)

SELECT count(*) AS deleted FROM deleted
