-- Port of plan/queries/plan_read_tasks.cypher: the persisted tasks of one
-- project, ordered by task id.
WITH picked AS (
    SELECT
        n.props,
        n.props ->> 'task_id' AS sort_key
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'foreign'
        AND n.labels[1] = 'Task'
)

SELECT
    props -> 'task_id' AS task_id,
    props -> 'area_id' AS area_id,
    props -> 'action' AS action,
    props -> 'revision' AS revision,
    props -> 'story_id' AS story_id,
    props -> 'phase' AS phase,
    props -> 'title' AS title,
    props -> 'description' AS description,
    props -> 'done_when' AS done_when,
    props -> 'complexity' AS complexity,
    props -> 'implements' AS implements,
    props -> 'release' AS release,
    props -> 'has_open_questions' AS has_open_questions,
    props -> 'planned_by' AS planned_by,
    props -> 'prompt_ref' AS prompt_ref,
    props -> 'spec_build_id' AS spec_build_id,
    props -> 'status' AS status,
    props -> 'claimed_by' AS claimed_by,
    props -> 'claimed_at' AS claimed_at,
    props -> 'attempts' AS attempts,
    props -> 'commit' AS commit,
    props -> 'block_reason' AS block_reason,
    props -> 'note' AS note,
    props -> 'created_at' AS created_at,
    props -> 'updated_at' AS updated_at,
    props -> 'created_run_id' AS created_run_id,
    props -> 'last_run_id' AS last_run_id
FROM picked
ORDER BY sort_key COLLATE "C"
