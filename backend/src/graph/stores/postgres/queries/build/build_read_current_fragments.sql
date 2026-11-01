-- Diff: the current fragments of the project.
WITH found AS (
    SELECT
        n.fragment_id,
        n.props -> 'document_id' AS document_id,
        n.props -> 'spec_id' AS spec_id,
        n.props -> 'heading_path' AS heading_path,
        n.props -> 'ordinal' AS ordinal,
        n.props -> 'line_start' AS line_start,
        n.props -> 'line_end' AS line_end,
        n.props -> 'content_hash' AS content_hash,
        n.props -> 'status' AS status,
        n.props -> 'extracted_hash' AS extracted_hash,
        n.props -> 'error_code' AS error_code
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'fragment'
        AND n.fragment_id IS NOT NULL
)

SELECT
    fragment_id,
    document_id,
    spec_id,
    heading_path,
    ordinal,
    line_start,
    line_end,
    content_hash,
    status,
    extracted_hash,
    error_code
FROM found
ORDER BY fragment_id COLLATE "C"
