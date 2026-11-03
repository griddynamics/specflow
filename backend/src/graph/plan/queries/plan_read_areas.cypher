// The persisted work areas of one project, ordered by area id.
MATCH (a:WorkArea)
WHERE a.group_id = $project_id
RETURN a.area_id AS area_id, a.kind AS kind, a.anchor AS anchor, a.title AS title, a.description AS description,
       a.interfaces AS interfaces, a.not_buildable AS not_buildable, a.planned_by AS planned_by, a.status AS status,
       a.created_at AS created_at, a.updated_at AS updated_at, a.last_run_id AS last_run_id
ORDER BY area_id
