// ArcadeDB override of core/queries/upsert_spec_node.cypher.
// The domain label is static (__LABEL__, ontology-validated): ArcadeDB silently ignores a dynamic label in
// MERGE/CREATE, and a label written to a node created in the same transaction fails. spec_key is the derived
// single-property unique key of (group_id, spec_id) (NULL_STRATEGY SKIP), set whenever spec_id is.
// The node read first is updated, or created when there is none (ArcadeDB 26.10 scans for a MERGE with two labels).
OPTIONAL MATCH (old:SpecNode {group_id: $project_id, uuid: $key})
WITH old, old IS NOT NULL AND NOT $label IN labels(old) AS label_mismatch, old IS NOT NULL AND old.spec_id IS NOT NULL AND 'spec_id' IN keys($props) AND coalesce($props.spec_id <> old.spec_id, true) AS spec_id_changed
FOREACH (_ IN CASE WHEN label_mismatch OR spec_id_changed OR old IS NOT NULL THEN [] ELSE [1] END | CREATE (n:SpecNode:__LABEL__ {group_id: $project_id, uuid: $key}) SET n += $props, n.spec_key = CASE WHEN 'spec_id' IN keys($props) THEN $project_id + ':' + $props.spec_id ELSE null END)
FOREACH (_ IN CASE WHEN NOT label_mismatch AND NOT spec_id_changed AND old IS NOT NULL THEN [1] ELSE [] END | SET old += $props, old.spec_key = CASE WHEN 'spec_id' IN keys($props) THEN $project_id + ':' + $props.spec_id ELSE old.spec_key END)
RETURN label_mismatch, spec_id_changed
