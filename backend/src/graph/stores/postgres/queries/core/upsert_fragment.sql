-- Create or update one Fragment by its fragment_id (unique per project).
-- Properties merge as SET += does: a null value removes the property; the key
-- is not part of :props.
WITH written AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    VALUES (
        :project_id,
        'fragment',
        ARRAY['Fragment'],
        graph.props_merge(
            cast(:props AS jsonb) - 'fragment_id',
            jsonb_build_object('fragment_id', cast(:key AS text))
        )
    )
    ON CONFLICT (group_id, fragment_id) WHERE kind = 'fragment'
    DO UPDATE
        SET
            props
            = graph.props_merge(
                node.props, cast(:props AS jsonb) - 'fragment_id'
            )
    RETURNING node.id
)

SELECT count(*) AS written FROM written
