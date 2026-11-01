-- Fragment: the foreign node of the project with the label in the parameter
-- the token SHAPE names and the uuid in the one the token KEY names.
SELECT n.id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'foreign'
    AND n.labels[1] = :__SHAPE__
    AND n.uuid = :__KEY__
