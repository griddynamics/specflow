-- Number of spec nodes of the project with any of :labels (the node filter of
-- read_nodes.sql, without :where).
SELECT count(*) AS total
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.spec_id IS NOT NULL
    AND n.labels && cast(:labels AS text[])
