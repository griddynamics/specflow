// Create or update one spec node by uuid; one row always.
// label_mismatch: a node with this uuid has another domain label.
// spec_id_changed: it has a spec_id and $props would set another one (or remove it).
// Either way nothing is written. Both lookups seek the (group_id, uuid) unique index.
OPTIONAL MATCH (old:SpecNode {group_id: $project_id, uuid: $key})
WITH old IS NOT NULL AND NOT $label IN labels(old) AS label_mismatch, old IS NOT NULL AND old.spec_id IS NOT NULL AND 'spec_id' IN keys($props) AND coalesce($props.spec_id <> old.spec_id, true) AS spec_id_changed
FOREACH (_ IN CASE WHEN label_mismatch OR spec_id_changed THEN [] ELSE [1] END | MERGE (n:SpecNode {group_id: $project_id, uuid: $key}) ON CREATE SET n:$($label) SET n += $props)
RETURN label_mismatch, spec_id_changed
