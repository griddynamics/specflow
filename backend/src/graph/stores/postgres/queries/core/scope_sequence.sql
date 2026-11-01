-- Port of core/queries/scope_sequence.cypher: nodes reaching the root by one
-- edge of each relation of a sequence, in order. Parameters: project_id,
-- spec_id, types (text[], the relations in sequence order), either_way
-- (boolean[], one flag per relation: walked either way), labels (text[]),
-- roots (text[]).
-- The walk starts at the root and takes the last relation first. A directed
-- relation is followed from its source to its target unless its flag is set
-- (then either way); an edge is used once per path; the nodes between the
-- ends are spec nodes without a spine-root label.
WITH RECURSIVE root AS (
    SELECT n.id
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.spec_id = :spec_id
),

steps AS (
    SELECT
        t.step_type,
        t.either_way,
        t.ord
    FROM unnest(
        cast(:types AS text[]), cast(:either_way AS boolean[])
    ) WITH ORDINALITY AS t (step_type, either_way, ord)
),

walk (node_id, depth, edge_ids) AS (
    SELECT
        root.id,
        0,
        cast(ARRAY[] AS bigint[])
    FROM root
    UNION ALL
    SELECT
        hop.prev_id,
        w.depth + 1,
        w.edge_ids || hop.edge_id
    FROM walk AS w
    INNER JOIN steps AS s ON s.ord = (SELECT count(*) FROM steps) - w.depth
    INNER JOIN graph.node AS c ON w.node_id = c.id
    CROSS JOIN
        LATERAL (
            SELECT
                e.id AS edge_id,
                e.source_id AS prev_id
            FROM graph.edge AS e
            WHERE
                e.target_id = w.node_id
                AND e.group_id = :project_id
                AND e.rel_type = s.step_type
            UNION ALL
            SELECT
                e.id AS edge_id,
                e.target_id AS prev_id
            FROM graph.edge AS e
            WHERE
                e.source_id = w.node_id
                AND e.source_id <> e.target_id
                AND e.group_id = :project_id
                AND e.rel_type = s.step_type
                AND s.either_way
        ) AS hop
    WHERE
        NOT (hop.edge_id = any(w.edge_ids))
        AND (
            w.depth = 0
            OR (
                c.group_id = :project_id
                AND c.kind = 'spec'
                AND NOT (c.labels && cast(:roots AS text[]))
            )
        )
)

SELECT DISTINCT n.uuid
FROM walk AS w
INNER JOIN graph.node AS n ON w.node_id = n.id
WHERE
    w.depth = (SELECT count(*) FROM steps)
    AND n.id NOT IN (SELECT root.id FROM root)
    AND n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.spec_id IS NOT NULL
    AND n.labels && cast(:labels AS text[])
