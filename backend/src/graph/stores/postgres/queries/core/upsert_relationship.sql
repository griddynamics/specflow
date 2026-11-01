-- Port of core/queries/upsert_relationship.cypher (__SOURCE__ and __TARGET__ are slots for the node matches rendered
-- from the match_*_node fragments by the dialect; their default is the match of a spec node). Params: project_id, s_key, s_label, t_key, t_label, rel_type, key, props.
-- No row when an end is missing. conflict = true when an edge of this type and edge_key joins other
-- endpoints; nothing is written then. Same endpoints, same key = merge props (SET r += props).
WITH s AS (
  /*__SOURCE__*/SELECT n.id FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :s_key AND CAST(:s_label AS text) = ANY (n.labels)/*__SOURCE__*/
), t AS (
  /*__TARGET__*/SELECT n.id FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :t_key AND CAST(:t_label AS text) = ANY (n.labels)/*__TARGET__*/
), ends AS (
  SELECT s.id AS sid, t.id AS tid FROM s, t
), other AS (
  SELECT count(*) AS conflicts
  FROM graph.edge e, ends
  WHERE e.group_id = :project_id AND e.type = :rel_type AND e.edge_key = :key
    AND NOT (e.source_id = ends.sid AND e.target_id = ends.tid)
), written AS (
  INSERT INTO graph.edge AS e (group_id, type, source_id, target_id, props)
  SELECT :project_id, CAST(:rel_type AS text), ends.sid, ends.tid,
         graph.props_merge(CAST(:props AS jsonb) - 'edge_key', jsonb_build_object('edge_key', CAST(:key AS text)))
  FROM ends, other WHERE other.conflicts = 0
  ON CONFLICT (group_id, type, edge_key)
  DO UPDATE SET props = graph.props_merge(e.props, CAST(:props AS jsonb) - 'edge_key')
  WHERE e.source_id = EXCLUDED.source_id AND e.target_id = EXCLUDED.target_id
  RETURNING 1
)
SELECT other.conflicts > 0 AS conflict FROM other, ends;
