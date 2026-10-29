// Infer: the swap's writes, one inferred edge per row.
// A row is skipped when an edge with its key exists that is not an inferred edge of the same rule, or,
// with row.either_direction, when the two nodes are already linked by a non-inferred edge of the relation.
UNWIND $rows AS row
MATCH (s:SpecNode {group_id: $project_id, uuid: row.source})
MATCH (t:SpecNode {group_id: $project_id, uuid: row.target})
WITH s, t, row
WHERE NOT EXISTS {
MATCH ()-[x:__TYPE__ {group_id: $project_id, edge_key: row.edge_key}]->()
WHERE coalesce(x.basis, '') <> $inferred OR coalesce(x.inference_rule, '') <> row.rule
} AND NOT (row.either_direction AND EXISTS { MATCH (s)-[y:__TYPE__]-(t) WHERE y.group_id = $project_id AND coalesce(y.basis, '') <> $inferred })
MERGE (s)-[r:__TYPE__ {group_id: $project_id, edge_key: row.edge_key}]->(t)
ON CREATE SET r.uuid = row.uuid, r.created_at = row.created_at
SET r += row.props
RETURN count(r) AS written, collect(row.ref) AS refs
