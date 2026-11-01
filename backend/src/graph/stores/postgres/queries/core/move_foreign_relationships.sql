-- Port of core/queries/move_foreign_relationships.cypher. Params: project_id, from_key, from_label, to_key,
-- to_label, owned_types (text[]).
-- Moves every relationship of old whose type is not owned onto new, keeping type, direction, props (the row is
-- re-pointed, so edge_key and id survive). Equal edge already on new, equal edge earlier in the moved set,
-- an edge between old and new are only deleted; a self-loop of old becomes a self-loop of new (canonical).
-- One row always: moved = every relationship taken off old.
WITH old AS (
  SELECT n.id FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :from_key AND CAST(:from_label AS text) = ANY (n.labels)
), new AS (
  SELECT n.id FROM graph.node n, old
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :to_key AND CAST(:to_label AS text) = ANY (n.labels)
    AND n.id <> old.id
), cand AS (
  SELECT e.id, e.type, e.props,
         CASE WHEN e.source_id = old.id THEN new.id ELSE e.source_id END AS new_src,
         CASE WHEN e.target_id = old.id THEN new.id ELSE e.target_id END AS new_tgt,
         (e.source_id = new.id OR e.target_id = new.id) AS between_ends
  FROM graph.edge e, old, new
  WHERE e.group_id = :project_id AND (e.source_id = old.id OR e.target_id = old.id)
    AND NOT (e.type = ANY (CAST(:owned_types AS text[])))
), ranked AS (
  SELECT c.*,
         CASE WHEN c.between_ends THEN false
              WHEN EXISTS (SELECT 1 FROM graph.edge x
                           WHERE x.group_id = :project_id AND x.source_id = c.new_src AND x.target_id = c.new_tgt
                             AND x.type = c.type AND x.props = c.props) THEN false
              ELSE row_number() OVER (PARTITION BY c.between_ends, c.new_src, c.new_tgt, c.type, c.props ORDER BY c.id) = 1
         END AS keep
  FROM cand c
), moved AS (
  UPDATE graph.edge e SET source_id = r.new_src, target_id = r.new_tgt
  FROM ranked r WHERE e.id = r.id AND r.keep
  RETURNING 1
), dropped AS (
  DELETE FROM graph.edge e USING ranked r WHERE e.id = r.id AND NOT r.keep
  RETURNING 1
)
SELECT (SELECT count(*) FROM ranked) AS moved;
