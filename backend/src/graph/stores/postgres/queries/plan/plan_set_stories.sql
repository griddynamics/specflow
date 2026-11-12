-- Port of plan/queries/plan_set_stories.cypher. Story calls: set properties of
-- stories by story id; a null property in the patch removes it. Rows: a JSON
-- list of id and properties; ids are unique within a call. Runs after
-- plan_lock_stories in the same transaction.
WITH r AS (
    SELECT
        e.item ->> 'id' AS id,
        coalesce(e.item -> 'properties', cast('{}' AS jsonb))
        || jsonb_build_object('updated_at', cast(:now AS text)) AS patch
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

updated AS (
    UPDATE graph.node
    SET props = graph.props_merge(node.props, r.patch)
    FROM r
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'Story'
        AND node.props ->> 'story_id' = r.id
    RETURNING node.id
)

SELECT count(*) AS written FROM updated
