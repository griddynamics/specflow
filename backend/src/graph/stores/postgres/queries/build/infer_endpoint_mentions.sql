-- Infer (endpoints): the targets mentioned in one of the subject's evidence
-- fragments, joined into infer_endpoint_subjects.
SELECT
    s.id AS sid,
    m.uuid AS target
FROM subj AS s
INNER JOIN
    graph.edge AS e
    ON s.id = e.source_id AND e.rel_type = 'REFERENCED_BY'
INNER JOIN graph.node AS f ON e.target_id = f.id
INNER JOIN graph.edge AS l ON f.id = l.target_id AND l.rel_type = 'MENTIONED_IN'
INNER JOIN graph.node AS m ON l.source_id = m.id
WHERE
    e.group_id = :project_id
    AND l.group_id = :project_id
    AND f.group_id = :project_id
    AND f.kind = 'fragment'
    AND f.props ->> 'status' = any(cast(:statuses AS text[]))
    AND m.group_id = :project_id
    AND m.kind = 'spec'
    AND m.uuid IS NOT NULL
    AND m.labels && cast(:target_labels AS text[])
