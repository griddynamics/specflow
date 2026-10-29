// Fulltext index page over fragments: fetched and hits of the project.
CALL db.index.fulltext.queryNodes($index, $query, {skip: $skip, limit: $page})
YIELD node, score
WITH collect({f: node, score: score}) AS raw
RETURN size(raw) AS fetched, [hit IN raw WHERE hit.f.group_id = $project_id AND hit.f.fragment_id IS NOT NULL AND hit.f.status = $active AND ($document_id IS NULL OR hit.f.document_id = $document_id) | {fragment_id: hit.f.fragment_id, score: hit.score}] AS hits
