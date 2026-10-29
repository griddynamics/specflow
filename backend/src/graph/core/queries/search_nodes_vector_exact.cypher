// Exact cosine over the project's own search vectors: used for a small candidate set, and as the
// fallback when the shared vector index does not yield enough hits of the project.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.embedding IS NOT NULL AND n.embedding_model = $model AND size(n.embedding) = size($vector) AND n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND all(name IN keys($where) WHERE n[name] = $where[name] OR (n[name] IS NULL AND $where[name] IS NULL))
WITH n, vector.similarity.cosine(n.embedding, $vector) AS score
RETURN n.uuid AS uuid, n.spec_id AS spec_id, score
ORDER BY score DESC, spec_id
LIMIT $limit
