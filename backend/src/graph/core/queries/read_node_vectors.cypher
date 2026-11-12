// The stored search vectors of the spec nodes of $uuids made by $model (similar, expand: no embedding call); a
// node without one is left out.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IN $uuids AND n.spec_id IS NOT NULL AND n.embedding IS NOT NULL AND n.embedding_model = $model
RETURN n.uuid AS uuid, n.spec_id AS spec_id, n.embedding AS embedding
ORDER BY spec_id
