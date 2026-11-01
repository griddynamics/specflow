-- Fragment: one spec node of the project by its uuid, joined into
-- upsert_relationship. KEY and LABEL are tokens for the names of the key and
-- label parameters; the domain label is checked.
SELECT n.id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.uuid = :__KEY__
    AND CAST(:__LABEL__ AS text) = ANY(n.labels)
