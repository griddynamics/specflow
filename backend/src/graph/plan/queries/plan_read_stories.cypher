// The persisted stories of one project, ordered by story id.
MATCH (s:Story)
WHERE s.group_id = $project_id
RETURN s.story_id AS story_id, s.area_id AS area_id, s.kind AS kind, s.anchor AS anchor, s.chapter AS chapter,
       s.title AS title, s.summary AS summary, s.prompt_ref AS prompt_ref, s.release AS release, s.status AS status,
       s.claimed_by AS claimed_by, s.claimed_at AS claimed_at, s.agent_label AS agent_label, s.branch AS branch,
       s.note AS note, s.pr_url AS pr_url, s.claim_token AS claim_token, s.token_holder AS token_holder,
       s.created_at AS created_at,
       s.updated_at AS updated_at, s.last_run_id AS last_run_id
ORDER BY story_id
