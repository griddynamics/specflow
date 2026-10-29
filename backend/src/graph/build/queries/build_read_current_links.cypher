// Diff: each node's REFERENCED_BY (with its evidence line text) and MENTIONED_IN links, one branch
// per type, each reading only the properties its writer sets (build_replace_node_links): lines on
// REFERENCED_BY, count on MENTIONED_IN; the other columns are null, as they read before
CALL () {
MATCH (n:SpecNode)-[r:REFERENCED_BY]->(f:Fragment)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL AND f.group_id = $project_id
RETURN n.uuid AS uuid, type(r) AS kind, f.fragment_id AS fragment_id, coalesce(f.document_id, '') AS document_id, r.line_start AS line_start, r.line_end AS line_end, null AS count, CASE WHEN f.text IS NOT NULL AND f.line_start IS NOT NULL AND r.line_start IS NOT NULL AND r.line_end IS NOT NULL THEN split(f.text, '\n')[r.line_start - f.line_start .. r.line_end - f.line_start + 1] ELSE [] END AS lines
UNION ALL
MATCH (n:SpecNode)-[m:MENTIONED_IN]->(f:Fragment)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL AND f.group_id = $project_id
RETURN n.uuid AS uuid, type(m) AS kind, f.fragment_id AS fragment_id, coalesce(f.document_id, '') AS document_id, null AS line_start, null AS line_end, m.count AS count, [] AS lines
}
RETURN uuid, kind, fragment_id, document_id, line_start, line_end, count, lines
ORDER BY uuid, kind, fragment_id, line_start
