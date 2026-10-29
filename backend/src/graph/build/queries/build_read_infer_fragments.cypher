// Infer: the text of fragments by id.
UNWIND $fragment_ids AS fragment_id
MATCH (f:Fragment {group_id: $project_id, fragment_id: fragment_id})
RETURN f.fragment_id AS fragment_id, coalesce(f.text, '') AS text
ORDER BY fragment_id
