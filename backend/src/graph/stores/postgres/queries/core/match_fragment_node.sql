-- Fragment: the Fragment of the project with fragment_id :__KEY__.
SELECT n.id
FROM graph.node AS n
WHERE n.group_id = :project_id AND n.kind = 'fragment' AND n.fragment_id = :__KEY__
