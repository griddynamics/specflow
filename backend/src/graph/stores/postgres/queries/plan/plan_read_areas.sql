-- Port of plan/queries/plan_read_areas.cypher: the persisted work areas of
-- one project, ordered by area id.
WITH picked AS (
    SELECT
        n.props,
        n.props ->> 'area_id' AS sort_key
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'foreign'
        AND n.labels[1] = 'WorkArea'
)

SELECT
    props -> 'area_id' AS area_id,
    props -> 'kind' AS kind,
    props -> 'anchor' AS anchor,
    props -> 'title' AS title,
    props -> 'description' AS description,
    props -> 'interfaces' AS interfaces,
    props -> 'not_buildable' AS not_buildable,
    props -> 'planned_by' AS planned_by,
    props -> 'prompt_ref' AS prompt_ref,
    props -> 'status' AS status,
    props -> 'created_at' AS created_at,
    props -> 'updated_at' AS updated_at,
    props -> 'last_run_id' AS last_run_id
FROM picked
ORDER BY sort_key COLLATE "C"
