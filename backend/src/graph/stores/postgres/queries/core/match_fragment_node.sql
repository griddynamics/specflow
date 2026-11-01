-- Fragment: the Fragment of the project with the fragment_id in the
-- parameter the token KEY names.
SELECT n.id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'fragment'
    AND n.fragment_id = :__KEY__
