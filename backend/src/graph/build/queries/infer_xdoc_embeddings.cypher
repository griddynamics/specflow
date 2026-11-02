// Infer (cross_document): the embedding of each node of $uuids, read once for every hub's member pairs
// (their cosines are computed in Python); a node missing or without an embedding is left out.
UNWIND $uuids AS uuid
MATCH (n:SpecNode {group_id: $project_id, uuid: uuid})
WHERE n.embedding IS NOT NULL
RETURN n.uuid AS uuid, n.embedding AS embedding
ORDER BY uuid
