// Verification: domain edges touching a spec node of the project (either end may be a fault).
MATCH (n:SpecNode)-[r]-()
WHERE n.group_id = $project_id AND r.group_id = $project_id AND type(r) IN $relations
WITH DISTINCT r
RETURN elementId(r) AS id, type(r) AS relation, r.uuid AS uuid, r.edge_key AS edge_key, r.basis AS basis, r.source_fragment_ids AS fragment_ids, r.inference_rule AS inference_rule, r.confidence AS confidence, r.evidence AS evidence, elementId(startNode(r)) AS source, elementId(endNode(r)) AS target
ORDER BY relation, id
