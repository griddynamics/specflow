-- Port of plan/queries/plan_read_links.cypher. One persisted plan relationship
-- type (:rel_type), with stored keys, endpoint ids (a spec node by spec id, a
-- plan node by its own id) and the other properties as [name, value] pairs.
WITH links AS (
    SELECT
        e.rel_type AS type,
        e.edge_key,
        CASE
            WHEN s.kind = 'spec' THEN s.spec_id
            WHEN s.labels[1] = 'Task' THEN s.props ->> 'task_id'
            WHEN s.labels[1] = 'WorkArea' THEN s.props ->> 'area_id'
            WHEN s.labels[1] = 'Story' THEN s.props ->> 'story_id'
        END AS source,
        CASE
            WHEN t.kind = 'spec' THEN t.spec_id
            WHEN t.labels[1] = 'Task' THEN t.props ->> 'task_id'
            WHEN t.labels[1] = 'WorkArea' THEN t.props ->> 'area_id'
            WHEN t.labels[1] = 'Story' THEN t.props ->> 'story_id'
        END AS target,
        coalesce(
            (
                SELECT
                    jsonb_agg(
                        jsonb_build_array(p.key, p.value) ORDER BY p.key
                    )
                FROM jsonb_each(e.props - 'edge_key') AS p
            ),
            cast('[]' AS jsonb)
        ) AS properties
    FROM graph.edge AS e
    INNER JOIN graph.node AS s
        ON e.source_id = s.id AND s.group_id = :project_id
    INNER JOIN graph.node AS t
        ON e.target_id = t.id AND t.group_id = :project_id
    WHERE e.group_id = :project_id AND e.rel_type = :rel_type
)

SELECT
    type,
    source,
    target,
    edge_key,
    properties
FROM links
ORDER BY edge_key COLLATE "C"
