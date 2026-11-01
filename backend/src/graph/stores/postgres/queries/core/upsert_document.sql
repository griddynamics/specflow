-- Create or update one Document by its document_id (unique per project).
-- Properties are merged (SET += ): a null value removes the property; the key is not part of :props.
WITH written AS (
    INSERT INTO graph.node AS n (group_id, kind, labels, props)
    VALUES (
        :project_id,
        'document',
        ARRAY['Document'],
        graph.props_merge(
            CAST(:props AS jsonb) - 'document_id',
            jsonb_build_object('document_id', CAST(:key AS text))
        )
    )
    ON CONFLICT (group_id, document_id) WHERE kind = 'document'
    DO UPDATE SET props = graph.props_merge(n.props, CAST(:props AS jsonb) - 'document_id')
    RETURNING n.id
)

SELECT count(*) AS written FROM written
