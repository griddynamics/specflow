// Infer: the swap's deletes, the inferred edge row.uuid of rule row.rule under row.edge_key.
UNWIND $rows AS row
MATCH ()-[r:__TYPE__ {group_id: $project_id, edge_key: row.edge_key}]->()
WHERE r.basis = $inferred AND r.inference_rule = row.rule AND (r.uuid = row.uuid OR r.uuid IS NULL)
DELETE r
RETURN count(*) AS written, collect(row.ref) AS refs
