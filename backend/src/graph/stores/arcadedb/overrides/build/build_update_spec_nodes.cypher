// ArcadeDB override of build/queries/build_update_spec_nodes.cypher: spec_key follows spec_id.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.uuid})
SET n += row.props, n.spec_key = CASE WHEN 'spec_id' IN keys(row.props) THEN $project_id + ':' + row.props.spec_id ELSE n.spec_key END
RETURN count(n) AS written
