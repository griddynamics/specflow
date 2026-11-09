-- Port of plan/queries/plan_read_stories.cypher: the persisted stories of one
-- project, ordered by story id.
WITH picked AS (
    SELECT
        n.props,
        n.props ->> 'story_id' AS sort_key
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'foreign'
        AND n.labels[1] = 'Story'
)

SELECT
    props -> 'story_id' AS story_id,
    props -> 'area_id' AS area_id,
    props -> 'kind' AS kind,
    props -> 'anchor' AS anchor,
    props -> 'chapter' AS chapter,
    props -> 'title' AS title,
    props -> 'summary' AS summary,
    props -> 'prompt_ref' AS prompt_ref,
    props -> 'release' AS release,
    props -> 'status' AS status,
    props -> 'created_at' AS created_at,
    props -> 'updated_at' AS updated_at,
    props -> 'last_run_id' AS last_run_id
FROM picked
ORDER BY sort_key COLLATE "C"
