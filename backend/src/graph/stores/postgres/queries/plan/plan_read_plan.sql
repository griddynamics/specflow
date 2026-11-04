-- Port of plan/queries/plan_read_plan.cypher: the Plan node of one project,
-- read for the current plan and mode selection. A missing property is null.
WITH picked AS (
    SELECT
        n.props,
        n.props ->> 'plan_id' AS sort_key
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'foreign'
        AND n.labels[1] = 'Plan'
)

SELECT
    props -> 'plan_id' AS plan_id,
    props -> 'profile' AS profile,
    props -> 'last_build_id' AS last_build_id,
    props -> 'consumed_build_ids' AS consumed_build_ids,
    props -> 'graph_fingerprint' AS graph_fingerprint,
    props -> 'ontology_version' AS ontology_version,
    props -> 'plan_ontology_version' AS plan_ontology_version,
    props -> 'rules_version' AS rules_version,
    props -> 'applying' AS applying,
    props -> 'last_run_id' AS last_run_id,
    props -> 'created_at' AS created_at,
    props -> 'updated_at' AS updated_at
FROM picked
ORDER BY sort_key COLLATE "C"
