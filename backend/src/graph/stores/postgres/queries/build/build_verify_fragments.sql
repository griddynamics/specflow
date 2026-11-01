-- Verification: fragment status and hashes (never the text), and whether any
-- node is grounded in them.
WITH found AS (
    SELECT
        f.fragment_id,
        f.props ->> 'document_id' AS document_id,
        f.props -> 'status' AS status,
        f.props -> 'content_hash' AS content_hash,
        f.props -> 'extracted_hash' AS extracted_hash,
        EXISTS(
            SELECT 1
            FROM graph.edge AS e
            INNER JOIN graph.node AS n ON e.source_id = n.id
            WHERE
                e.target_id = f.id
                AND e.rel_type = 'REFERENCED_BY'
                AND n.group_id = :project_id
                AND n.kind = 'spec'
        ) AS grounds
    FROM graph.node AS f
    WHERE f.group_id = :project_id AND f.kind = 'fragment'
)

SELECT
    fragment_id,
    document_id,
    status,
    content_hash,
    extracted_hash,
    grounds
FROM found
ORDER BY document_id COLLATE "C", fragment_id COLLATE "C"
