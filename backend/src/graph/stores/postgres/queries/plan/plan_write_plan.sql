-- Port of plan/queries/plan_write_plan.cypher. Persist, last batch: the Plan
-- node's properties are written and the applying run id is cleared (a JSON
-- null in the patch removes the property).
WITH written AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            cast(:properties AS jsonb)
            || jsonb_build_object(
                'updated_at', cast(:now AS text),
                'last_run_id', cast(:run_id AS text),
                'applying', NULL
            )
        )
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'Plan'
        AND node.props ->> 'plan_id' = :plan_id
    RETURNING node.props ->> 'plan_id' AS plan_id
)

SELECT written.plan_id FROM written
