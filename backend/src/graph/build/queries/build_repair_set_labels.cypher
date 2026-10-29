// Repair: set the kept domain label and label_disagreement of nodes, removing their other build-owned labels.
UNWIND $rows AS row
MATCH (n:SpecNode)
WHERE elementId(n) = row.id AND n.group_id = $project_id
REMOVE n:$(row.remove)
SET n:$(row.label), n.label_disagreement = row.label_disagreement
RETURN count(n) AS written
