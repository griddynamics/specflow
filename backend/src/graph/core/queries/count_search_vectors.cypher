// matching: candidates whose search vector was made by $model; stored: all with one.
MATCH (n:SpecNode)
WHERE n.embedding IS NOT NULL AND n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND all(name IN keys($where) WHERE n[name] = $where[name] OR (n[name] IS NULL AND $where[name] IS NULL))
RETURN count(CASE WHEN n.embedding_model = $model THEN 1 END) AS matching, count(n) AS stored
