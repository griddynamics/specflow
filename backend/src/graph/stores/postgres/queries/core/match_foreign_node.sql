-- Fragment: the foreign node of the project with label :__SHAPE__ and uuid :__KEY__.
SELECT n.id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'foreign'
    AND n.labels[1] = :__SHAPE__
    AND n.uuid = :__KEY__
