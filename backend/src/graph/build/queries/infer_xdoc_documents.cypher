// Infer (cross_document): the title of each document of $document_ids, else its path, else its id.
UNWIND $document_ids AS document_id
MATCH (d:Document {group_id: $project_id, document_id: document_id})
RETURN d.document_id AS document_id, CASE WHEN coalesce(d.title, '') <> '' THEN d.title WHEN coalesce(d.path, '') <> '' THEN d.path ELSE d.document_id END AS title
ORDER BY document_id
