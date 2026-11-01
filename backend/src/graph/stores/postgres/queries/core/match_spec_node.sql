-- Fragment: one spec node of the project by :__KEY__ (the key is its uuid); joined into upsert_relationship.
-- __KEY__ and __LABEL__ are the names of the key and label parameters; the domain label is checked.
SELECT n.id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.uuid = :__KEY__
    AND CAST(:__LABEL__ AS text) = any(n.labels)
