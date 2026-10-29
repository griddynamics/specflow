// Persist: replace the evidence and mention links of nodes in a batch.
// A missing fragment is created as a key-only stub: fragments are written in the last phase.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.uuid})
CALL (n) {
OPTIONAL MATCH (n)-[o:REFERENCED_BY|MENTIONED_IN]->(:Fragment)
DELETE o
}
CALL (n, row) {
UNWIND row.evidence AS ev
MERGE (f:Fragment {group_id: $project_id, fragment_id: ev.fragment_id})
CREATE (n)-[:REFERENCED_BY {group_id: $project_id, line_start: ev.line_start, line_end: ev.line_end}]->(f)
}
CALL (n, row) {
UNWIND row.mentions AS m
MERGE (f:Fragment {group_id: $project_id, fragment_id: m.fragment_id})
CREATE (n)-[:MENTIONED_IN {group_id: $project_id, count: m.count}]->(f)
}
RETURN count(n) AS written
