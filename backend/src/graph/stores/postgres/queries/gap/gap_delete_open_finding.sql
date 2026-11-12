-- Port of gap/queries/gap_delete_open_finding.cypher. Delete one open finding;
-- its edges cascade. deleted is 0 when it is gone or closed.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'Finding'
        AND node.props ->> 'finding_id' = :finding_id
        AND node.props ->> 'status' = 'open'
    RETURNING node.id
)

SELECT count(*) AS deleted FROM deleted
