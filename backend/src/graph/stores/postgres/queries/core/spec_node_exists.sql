-- Whether one spec node of the project with the domain label exists.
SELECT count(*) AS found
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.uuid = :n_key
    AND cast(:n_label AS text) = any(n.labels)
