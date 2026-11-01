-- Port of core/queries/scope_via.cypher: nodes reaching the root by MIN_HOPS
-- to MAX_HOPS edges of any of the relation types. Parameters: project_id,
-- spec_id, types (text[]), labels (text[]), symmetric (text[]), roots
-- (text[]), max_hops (int). The slots MIN_HOPS and MAX_HOPS hold the hop
-- counts (defaults 1 and 3; the literal upper bound is the walk's hard cap,
-- :max_hops its effective bound, policy.capped_traversals in
-- graph-schema.yaml). Returns each uuid once.
-- The walk starts at the root and steps backwards: hop i leaves nodes[i], so
-- a directed edge is followed from its source to its target unless its type
-- is symmetric (then either way). It uses edges of the project only, no edge
-- twice on one path (relationship uniqueness) and never expands through a
-- node with a spine-root label unless that is the root itself.
WITH RECURSIVE root AS (
    SELECT n.id
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.spec_id = :spec_id
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
                AND e.rel_type = any(cast(:types AS text[]))
            UNION ALL
            SELECT
                e.id AS edge_id,
                e.target_id AS prev_id
            FROM graph.edge AS e
            WHERE
                e.source_id = w.node_id
                AND e.source_id <> e.target_id
                AND e.group_id = :project_id
                AND e.rel_type = any(cast(:types AS text[]))
                AND e.rel_type = any(cast(:symmetric AS text[]))
        ) AS hop
    WHERE
        w.depth < /*__MAX_HOPS__*/3/*__MAX_HOPS__*/
        AND NOT (hop.edge_id = any(w.edge_ids))
        AND (
            w.depth = 0
            OR NOT (c.labels && cast(:roots AS text[]))
            OR c.id IN (SELECT root.id FROM root)
        )
)

SELECT DISTINCT n.uuid
FROM walk AS w
INNER JOIN graph.node AS n ON w.node_id = n.id
WHERE
    w.depth BETWEEN /*__MIN_HOPS__*/1/*__MIN_HOPS__*/
    AND /*__MAX_HOPS__*/3/*__MAX_HOPS__*/
    AND w.depth <= :max_hops
    AND n.id NOT IN (SELECT root.id FROM root)
    AND n.group_id = :project_id
    AND n.spec_id IS NOT NULL
    AND n.labels && cast(:labels AS text[])
