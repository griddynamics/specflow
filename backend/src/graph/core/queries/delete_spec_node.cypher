// Delete one spec node of the project by uuid when it carries the domain label.
MATCH (n:SpecNode {group_id: $project_id, uuid: $key})
WHERE $label IN labels(n)
DETACH DELETE n
RETURN count(*) AS deleted
