-- Port of plan/queries/plan_delete_links.cypher. Persist: delete plan
-- relationships of one type (:rel_type) by stored key and endpoint ids (a spec
-- end by spec id, a plan end by its own id). Rows: a JSON list of key, source
-- and target.
WITH r AS (
    SELECT
        j.item ->> 'key' AS edge_key,
        j.item ->> 'source' AS source,
        j.item ->> 'target' AS target
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS j (item)
),

found AS (
    SELECT e.id
    FROM r
    INNER JOIN graph.edge AS e
        ON
            e.group_id = :project_id
            AND e.rel_type = :rel_type
            AND r.edge_key = e.edge_key
    INNER JOIN graph.node AS s ON e.source_id = s.id
    INNER JOIN graph.node AS t ON e.target_id = t.id
    WHERE
        coalesce(
            s.props ->> 'task_id',
            s.props ->> 'story_id',
            s.props ->> 'area_id',
            s.spec_id
        ) = r.source
        AND coalesce(
            t.props ->> 'task_id',
            t.props ->> 'story_id',
            t.props ->> 'area_id',
            t.spec_id
        ) = r.target
),

deleted AS (
    DELETE FROM graph.edge
    USING found
    WHERE edge.id = found.id
    RETURNING edge.id
)

SELECT count(*) AS deleted FROM deleted
