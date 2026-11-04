-- Plan: the persisted fragments a revision is matched with; retired fragments
-- are never matched.
WITH found AS (
    SELECT
        n.fragment_id,
        n.props ->> 'document_id' AS document_id,
        coalesce(n.props -> 'heading_path', cast('[]' AS jsonb))
            AS heading_path,
        coalesce(n.props -> 'ordinal', to_jsonb(0)) AS ordinal,
        coalesce(n.props -> 'line_start', to_jsonb(0)) AS line_start,
        coalesce(n.props -> 'line_end', to_jsonb(0)) AS line_end,
        coalesce(n.props ->> 'text', '') AS text,
        coalesce(n.props ->> 'content_hash', '') AS content_hash,
        coalesce(n.props ->> 'status', 'active') AS status,
        n.props -> 'extracted_hash' AS extracted_hash,
        n.props -> 'error_code' AS error_code
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'fragment'
        AND n.fragment_id IS NOT NULL
        AND n.props ->> 'document_id' IS NOT NULL
        AND coalesce(n.props ->> 'status', 'active') <> 'retired'
)

SELECT
    fragment_id,
    document_id,
    heading_path,
    ordinal,
    line_start,
    line_end,
    text,
    content_hash,
    status,
    extracted_hash,
    error_code
FROM found
ORDER BY document_id COLLATE "C", ordinal, fragment_id COLLATE "C"
