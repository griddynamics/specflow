-- Every document of the project.
WITH found AS (
    SELECT
        n.document_id,
        n.props ->> 'path' AS doc_path,
        n.props || jsonb_build_object('group_id', n.group_id) AS props
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'document'
        AND n.document_id IS NOT NULL
)

SELECT props
FROM found
ORDER BY doc_path COLLATE "C", document_id COLLATE "C"
