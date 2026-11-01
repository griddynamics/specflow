-- Verification: domain edges touching a spec node of the project (either end
-- may be a fault). id, source and target are the text of the edge and node
-- ids.
WITH touching AS (
    SELECT e.id AS edge_id
    FROM graph.node AS n
    INNER JOIN graph.edge AS e ON n.id = e.source_id
    WHERE n.group_id = :project_id AND n.kind = 'spec'
    UNION
    SELECT e.id AS edge_id
    FROM graph.node AS n
    INNER JOIN graph.edge AS e ON n.id = e.target_id
    WHERE n.group_id = :project_id AND n.kind = 'spec'
),

found AS (
    SELECT
        cast(r.id AS text) AS id,
        r.rel_type AS relation,
        cast(r.source_id AS text) AS source,
        cast(r.target_id AS text) AS target,
        r.props -> 'uuid' AS uuid,
        r.props -> 'edge_key' AS edge_key,
        r.props -> 'basis' AS basis,
        r.props -> 'source_fragment_ids' AS fragment_ids,
        r.props -> 'inference_rule' AS inference_rule,
        r.props -> 'confidence' AS confidence,
        r.props -> 'evidence' AS evidence
    FROM touching AS t
    INNER JOIN graph.edge AS r ON t.edge_id = r.id
    WHERE
        r.group_id = :project_id
        AND r.rel_type = any(cast(:relations AS text[]))
)

SELECT
    id,
    relation,
    uuid,
    edge_key,
    basis,
    fragment_ids,
    inference_rule,
    confidence,
    evidence,
    source,
    target
FROM found
ORDER BY relation COLLATE "C", id COLLATE "C"
