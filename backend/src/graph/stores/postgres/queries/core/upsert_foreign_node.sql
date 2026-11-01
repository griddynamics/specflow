-- Create or update a foreign node (a declared label of another owner than build) by its uuid.
-- The label is :foreign_label (a bind value); the (group_id, label, uuid) unique index of foreign nodes is its key.
WITH written AS (
    INSERT INTO graph.node AS n (group_id, kind, labels, props)
    VALUES (
        :project_id,
        'foreign',
        ARRAY[CAST(:foreign_label AS text)],
        graph.props_merge(
            CAST(:props AS jsonb) - 'uuid',
            jsonb_build_object('uuid', CAST(:key AS text))
        )
    )
    ON CONFLICT (group_id, (labels[1]), uuid) WHERE kind = 'foreign'
    DO UPDATE SET props = graph.props_merge(n.props, CAST(:props AS jsonb) - 'uuid')
    RETURNING n.id
)

SELECT count(*) AS written FROM written
