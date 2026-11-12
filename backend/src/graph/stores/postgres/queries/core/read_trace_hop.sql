-- Trace (trace.md "Library"): the domain edges of :relations at each spec
-- node of :frontier (by uuid), with the other end, whether the edge leaves the
-- frontier node, and the edge's basis, confidence and rule.
WITH around AS (
    SELECT
        s.uuid AS frontier,
        e.id AS edge_id,
        e.target_id AS other_id,
        TRUE AS outgoing
    FROM graph.node AS s
    INNER JOIN graph.edge AS e ON s.id = e.source_id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(cast(:frontier AS text[]))
        AND e.group_id = :project_id
        AND e.rel_type = any(cast(:relations AS text[]))
    UNION ALL
    SELECT
        s.uuid AS frontier,
        e.id AS edge_id,
        e.source_id AS other_id,
        FALSE AS outgoing
    FROM graph.node AS s
    INNER JOIN graph.edge AS e ON s.id = e.target_id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(cast(:frontier AS text[]))
        AND e.group_id = :project_id
        AND e.rel_type = any(cast(:relations AS text[]))
),

found AS (
    SELECT
        around.frontier,
        m.uuid,
        m.spec_id,
        m.labels,
        e.rel_type AS relation,
        around.outgoing,
        m.props ->> 'name' AS name,
        e.props ->> 'basis' AS basis,
        e.props ->> 'inference_rule' AS inference_rule,
        e.props -> 'confidence' AS confidence
    FROM around
    INNER JOIN graph.edge AS e ON around.edge_id = e.id
    INNER JOIN graph.node AS m ON around.other_id = m.id
    WHERE
        m.group_id = :project_id
        AND m.kind = 'spec'
        AND m.spec_id IS NOT NULL
        AND m.labels && cast(:labels AS text[])
)

SELECT
    frontier,
    uuid,
    spec_id,
    labels,
    name,
    relation,
    outgoing,
    basis,
    confidence,
    inference_rule
FROM found
ORDER BY frontier COLLATE "C", spec_id COLLATE "C", relation COLLATE "C"
