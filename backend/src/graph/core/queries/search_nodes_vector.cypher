// Vector index page: fetched and hits of the project. An approximate index may return fewer than $k.
// Written in Cypher 5, the only language of the deployed Neo4j 5.26 (docker-compose.yml), which has no
// SEARCH clause: the vector index is queried through its procedure, also on a newer server.
CYPHER 5
CALL db.index.vector.queryNodes($index, $k, $vector)
YIELD node, score
WITH collect({n: node, score: score}) AS raw
RETURN size(raw) AS fetched, [hit IN raw WHERE hit.n.embedding_model = $model AND hit.n.group_id = $project_id AND hit.n.spec_id IS NOT NULL AND any(label IN labels(hit.n) WHERE label IN $labels) AND all(name IN keys($where) WHERE hit.n[name] = $where[name] OR (hit.n[name] IS NULL AND $where[name] IS NULL)) | {uuid: hit.n.uuid, spec_id: hit.n.spec_id, score: hit.score}][..$limit] AS hits
