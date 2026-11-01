-- Fragment: one spec node of the project by :__KEY__ (its uuid), whatever its domain label.
SELECT n.id
FROM graph.node AS n
WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :__KEY__
