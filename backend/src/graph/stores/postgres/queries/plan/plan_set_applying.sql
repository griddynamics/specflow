-- Port of plan/queries/plan_set_applying.cypher. Persist, first batch: the
-- project's Plan node exists and holds the applying run id, so claims wait.
-- The plan's uuid is derived from its plan id, so the foreign node key
-- (group_id, label, uuid) is the MERGE key; created_at is set on create only.
WITH written AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    VALUES (
        :project_id,
        'foreign',
        ARRAY['Plan'],
        jsonb_build_object(
            'uuid', cast(:uuid AS text),
            'plan_id', cast(:plan_id AS text),
            'created_at', cast(:now AS text),
            'applying', cast(:run_id AS text),
            'updated_at', cast(:now AS text)
        )
    )
    ON CONFLICT (group_id, (labels[1]), uuid) WHERE kind = 'foreign'
    DO UPDATE
        SET
            props = graph.props_merge(
                node.props,
                jsonb_build_object(
                    'applying', cast(:run_id AS text),
                    'updated_at', cast(:now AS text)
                )
            )
    RETURNING node.props ->> 'plan_id' AS plan_id
)

SELECT written.plan_id FROM written
