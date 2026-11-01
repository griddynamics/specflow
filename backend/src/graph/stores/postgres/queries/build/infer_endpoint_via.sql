-- Infer (endpoints): the targets a subject reaches over one edge of a via
-- relation, joined into infer_endpoint_subjects. The relation is the parameter
-- the token TYPE names; the slot SYMMETRIC is a boolean literal: TRUE walks
-- the edge either way (default FALSE: directed).
SELECT
    s.id AS sid,
    m.uuid AS target
FROM subj AS s
INNER JOIN graph.edge AS r ON s.id = r.source_id
INNER JOIN graph.node AS m ON r.target_id = m.id
WHERE
    r.rel_type = :__TYPE__
    AND r.group_id = :project_id
    AND coalesce(r.props ->> 'basis', 'stated') = any(cast(:bases AS text[]))
    AND m.group_id = :project_id
    AND m.kind = 'spec'
    AND m.uuid IS NOT NULL
    AND m.labels && cast(:target_labels AS text[])
UNION
SELECT
    s.id AS sid,
    m.uuid AS target
FROM subj AS s
INNER JOIN graph.edge AS r ON s.id = r.target_id
INNER JOIN graph.node AS m ON r.source_id = m.id
WHERE
    /*__SYMMETRIC__*/FALSE/*__SYMMETRIC__*/
    AND r.rel_type = :__TYPE__
    AND r.group_id = :project_id
    AND coalesce(r.props ->> 'basis', 'stated') = any(cast(:bases AS text[]))
    AND m.group_id = :project_id
    AND m.kind = 'spec'
    AND m.uuid IS NOT NULL
    AND m.labels && cast(:target_labels AS text[])
