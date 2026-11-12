-- Port of gap/queries/gap_set_open_finding.cypher. Set properties of one open
-- finding by finding id; a null property in the patch removes it. written is
-- 0 when the finding is gone or closed.
WITH updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            cast(:props AS jsonb)
            || jsonb_build_object('updated_at', cast(:now AS text))
        )
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'Finding'
        AND node.props ->> 'finding_id' = :finding_id
        AND node.props ->> 'status' = 'open'
    RETURNING node.id
)

SELECT count(*) AS written FROM updated
