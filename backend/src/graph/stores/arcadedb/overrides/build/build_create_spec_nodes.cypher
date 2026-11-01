// ArcadeDB override of build/queries/build_create_spec_nodes.cypher: one statement per domain label.
// The label is static (__LABEL__) in the MERGE; the rows are those of that label only. spec_key is the
// derived unique key of (group_id, spec_id).
UNWIND $rows AS row
MERGE (n:SpecNode:__LABEL__ {group_id: $project_id, uuid: row.uuid})
SET n += row.props, n.spec_key = CASE WHEN 'spec_id' IN keys(row.props) THEN $project_id + ':' + row.props.spec_id ELSE n.spec_key END
RETURN count(n) AS written
