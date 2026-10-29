// Repair: restore one REFERENCED_BY per evidence line range, to fragments that exist.
UNWIND $rows AS row
MATCH (n:SpecNode)
WHERE elementId(n) = row.id AND n.group_id = $project_id
CALL (n, row) {
UNWIND row.evidence AS ev
MATCH (f:Fragment {group_id: $project_id, fragment_id: ev.fragment_id})
MERGE (n)-[:REFERENCED_BY {group_id: $project_id, line_start: ev.line_start, line_end: ev.line_end}]->(f)
}
RETURN count(n) AS written
