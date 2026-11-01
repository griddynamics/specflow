-- The fragments of the project, optionally of one document.
WITH found AS (
    SELECT
        n.fragment_id,
        n.props ->> 'document_id' AS document_id,
        n.props -> 'ordinal' AS ordinal,
        n.props || jsonb_build_object('group_id', n.group_id) AS props
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'fragment'
        AND n.fragment_id IS NOT NULL
        AND (
            cast(:document_id AS text) IS NULL
            OR n.props ->> 'document_id' = cast(:document_id AS text)
        )
)

SELECT props
FROM found
ORDER BY document_id COLLATE "C", ordinal, fragment_id COLLATE "C"
