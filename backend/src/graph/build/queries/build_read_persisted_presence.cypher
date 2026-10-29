// Mode selection: whether anything of the project was persisted.
RETURN EXISTS {
MATCH (d:Document)
WHERE d.group_id = $project_id
} AS documents, EXISTS {
MATCH (n:SpecNode)
WHERE n.group_id = $project_id
} AS spec_nodes
