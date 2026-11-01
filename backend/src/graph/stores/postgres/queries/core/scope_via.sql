-- Port of core/queries/scope_via.cypher. Params: project_id, spec_id, types (text[]), labels (text[]),
-- symmetric (text[]), roots (text[]), max_hops (int). Slots MIN_HOPS / MAX_HOPS are rendered integers (the
-- walk's literal hard cap; validated 1..16 by the renderer). Returns distinct uuid.
-- The walk starts at the root and steps backwards: hop i leaves nodes[i], so a directed edge is followed from
-- its source to its target unless its type is symmetric (then either way). Edges of the project only, no
-- edge twice on one path (Cypher relationship uniqueness), never expand through a node with a spine-root label
-- unless it is the root itself.
WITH RECURSIVE root AS (
  SELECT n.id FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.spec_id = :spec_id
), walk (node_id, depth, edge_ids) AS (
  SELECT root.id, 0, ARRAY[]::bigint[] FROM root
  UNION ALL
  SELECT hop.prev_id, w.depth + 1, w.edge_ids || hop.edge_id
  FROM walk w
  JOIN graph.node c ON c.id = w.node_id
  CROSS JOIN LATERAL (
    SELECT e.id AS edge_id, e.source_id AS prev_id
    FROM graph.edge e
    WHERE e.target_id = w.node_id AND e.group_id = :project_id AND e.type = ANY (CAST(:types AS text[]))
    UNION ALL
    SELECT e.id, e.target_id
    FROM graph.edge e
    WHERE e.source_id = w.node_id AND e.source_id <> e.target_id AND e.group_id = :project_id
      AND e.type = ANY (CAST(:types AS text[])) AND e.type = ANY (CAST(:symmetric AS text[]))
  ) hop
  WHERE w.depth < /*__MAX_HOPS__*/3/*__MAX_HOPS__*/
    AND NOT (hop.edge_id = ANY (w.edge_ids))
    AND (w.depth = 0 OR NOT (c.labels && CAST(:roots AS text[])) OR c.id IN (SELECT id FROM root))
)
SELECT DISTINCT n.uuid
FROM walk w
JOIN graph.node n ON n.id = w.node_id
WHERE w.depth BETWEEN /*__MIN_HOPS__*/1/*__MIN_HOPS__*/ AND /*__MAX_HOPS__*/3/*__MAX_HOPS__*/
  AND w.depth <= :max_hops
  AND n.id NOT IN (SELECT id FROM root)
  AND n.group_id = :project_id AND n.spec_id IS NOT NULL
  AND n.labels && CAST(:labels AS text[]);
