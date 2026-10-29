// Whether one spec node of the project with the domain label exists.
MATCH (n:SpecNode {group_id: $project_id, uuid: $n_key})
WHERE $n_label IN labels(n)
RETURN count(n) AS found
