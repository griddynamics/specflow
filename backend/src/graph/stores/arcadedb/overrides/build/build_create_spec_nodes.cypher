// ArcadeDB override of build/queries/build_create_spec_nodes.cypher: one statement per domain label.
// The label is static (__LABEL__); the rows are those of that label only. spec_key is the derived unique key
// of (group_id, spec_id). ArcadeDB 26.10 does not plan a MERGE with two labels on the (group_id, uuid) index:
// each row scanned the label (about 40 ms a node, growing with the table). The existing node is matched
// instead, then created or updated; the rows of one batch have distinct uuids.
UNWIND $rows AS row
OPTIONAL MATCH (e:SpecNode:__LABEL__ {group_id: $project_id, uuid: row.uuid})
FOREACH (_ IN CASE WHEN e IS NULL THEN [1] ELSE [] END | CREATE (n:SpecNode:__LABEL__ {group_id: $project_id, uuid: row.uuid}) SET n += row.props, n.spec_key = CASE WHEN 'spec_id' IN keys(row.props) THEN $project_id + ':' + row.props.spec_id ELSE null END)
FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END | SET e += row.props, e.spec_key = CASE WHEN 'spec_id' IN keys(row.props) THEN $project_id + ':' + row.props.spec_id ELSE e.spec_key END)
RETURN count(row) AS written
