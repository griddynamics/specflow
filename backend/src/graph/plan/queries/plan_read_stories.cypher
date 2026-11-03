// The persisted stories of one project, ordered by story id.
MATCH (s:Story)
WHERE s.group_id = $project_id
RETURN s.story_id AS story_id, s.area_id AS area_id, s.kind AS kind, s.anchor AS anchor, s.chapter AS chapter,
       s.title AS title, s.summary AS summary, s.release AS release, s.status AS status,
       s.created_at AS created_at, s.updated_at AS updated_at, s.last_run_id AS last_run_id
ORDER BY story_id
