-- Plan: the persisted documents the snapshot is compared with; missing
-- properties are defaulted.
WITH found AS (
    SELECT
        n.document_id,
        coalesce(n.props ->> 'revision_id', '') AS revision_id,
        coalesce(n.props -> 'version', to_jsonb(0)) AS version,
        coalesce(n.props ->> 'path', n.document_id) AS path,
        coalesce(n.props ->> 'title', '') AS title,
        coalesce(n.props ->> 'status', 'active') AS status
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'document'
        AND n.document_id IS NOT NULL
)

SELECT
    document_id,
    revision_id,
    version,
    path,
    title,
    status
FROM found
ORDER BY path COLLATE "C", document_id COLLATE "C"
