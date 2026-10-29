SELECT
    substr(markdown, :start, :limit) AS chunk,
    length(markdown) AS total
FROM documents
WHERE id = :document_id AND project_id = :project_id
