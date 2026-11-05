// The Plan node of one project, read for the current plan and mode selection.
MATCH (p:Plan)
WHERE p.group_id = $project_id
RETURN p.plan_id AS plan_id, p.profile AS profile, p.last_build_id AS last_build_id,
       p.consumed_build_ids AS consumed_build_ids,
       p.graph_fingerprint AS graph_fingerprint, p.ontology_version AS ontology_version,
       p.plan_ontology_version AS plan_ontology_version, p.rules_version AS rules_version,
       p.applying AS applying, p.last_run_id AS last_run_id,
       p.created_at AS created_at, p.updated_at AS updated_at
ORDER BY plan_id
