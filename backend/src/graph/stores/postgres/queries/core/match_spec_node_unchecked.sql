-- Fragment: one spec node of the project by its uuid (the parameter named by
-- the token KEY), whatever its domain label.
SELECT n.id
FROM graph.node AS n
WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :__KEY__
