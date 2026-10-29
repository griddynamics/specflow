// Fulltext index page: fetched (before filtering) and hits (of the project, in score order).
CALL db.index.fulltext.queryNodes($index, $query, {skip: $skip, limit: $page})
YIELD node, score
WITH collect({n: node, score: score}) AS raw
RETURN size(raw) AS fetched, [hit IN raw WHERE hit.n.group_id = $project_id AND hit.n.spec_id IS NOT NULL AND any(label IN labels(hit.n) WHERE label IN $labels) AND all(name IN keys($where) WHERE hit.n[name] = $where[name] OR (hit.n[name] IS NULL AND $where[name] IS NULL)) | {uuid: hit.n.uuid, spec_id: hit.n.spec_id, score: hit.score}] AS hits
