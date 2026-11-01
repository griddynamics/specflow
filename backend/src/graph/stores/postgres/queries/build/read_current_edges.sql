-- Every edge of one domain relation in the project, with the uuid, identity
-- key and spec_id of both ends. The relation is the parameter :rel_type (a
-- bind value).
WITH found AS (
    SELECT
        r.edge_key,
        s.uuid AS source_uuid,
        t.uuid AS target_uuid,
        s.spec_id AS source_spec_id,
        t.spec_id AS target_spec_id,
        r.props || jsonb_build_object('group_id', r.group_id) AS props,
        s.props -> 'identity_key' AS source_key,
        t.props -> 'identity_key' AS target_key
    FROM graph.edge AS r
    INNER JOIN graph.node AS s ON r.source_id = s.id
    INNER JOIN graph.node AS t ON r.target_id = t.id
    WHERE
        r.group_id = :project_id
        AND r.rel_type = :rel_type
        AND r.edge_key IS NOT NULL
        AND s.kind = 'spec'
        AND t.kind = 'spec'
        AND s.group_id = :project_id
        AND t.group_id = :project_id
)

SELECT
    edge_key,
    props,
    source_uuid,
    target_uuid,
    source_key,
    target_key,
    source_spec_id,
    target_spec_id
FROM found
ORDER BY edge_key COLLATE "C", source_uuid COLLATE "C", target_uuid COLLATE "C"
