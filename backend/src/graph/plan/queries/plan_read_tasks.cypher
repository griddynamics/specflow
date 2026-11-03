// The persisted tasks of one project, ordered by task id.
MATCH (t:Task)
WHERE t.group_id = $project_id
RETURN t.task_id AS task_id, t.area_id AS area_id, t.action AS action, t.revision AS revision, t.story_id AS story_id,
       t.phase AS phase, t.title AS title, t.description AS description, t.done_when AS done_when,
       t.complexity AS complexity, t.implements AS implements, t.release AS release,
       t.has_open_questions AS has_open_questions, t.planned_by AS planned_by, t.prompt_ref AS prompt_ref,
       t.spec_build_id AS spec_build_id, t.status AS status, t.claimed_by AS claimed_by, t.claimed_at AS claimed_at,
       t.attempts AS attempts, t.commit AS commit, t.block_reason AS block_reason, t.note AS note,
       t.created_at AS created_at, t.updated_at AS updated_at, t.created_run_id AS created_run_id,
       t.last_run_id AS last_run_id
ORDER BY task_id
