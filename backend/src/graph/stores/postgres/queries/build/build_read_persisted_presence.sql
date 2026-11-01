-- Mode selection: whether anything of the project was persisted.
SELECT
    EXISTS(
        SELECT 1
        FROM graph.node AS d
        WHERE d.group_id = :project_id AND d.kind = 'document'
    ) AS documents,
    EXISTS(
        SELECT 1
        FROM graph.node AS n
        WHERE n.group_id = :project_id AND n.kind = 'spec'
    ) AS spec_nodes
