-- Port of plan/queries/plan_lock_stories.cypher. Story calls: lock the Story
-- rows of a claim unit (FOR UPDATE, in one fixed order for every caller), then
-- answer their stored properties, the claim token included, as they are after
-- any concurrent change commits.
SELECT
    n.props AS story,
    n.props ->> 'story_id' AS story_id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'foreign'
    AND n.labels[1] = 'Story'
    AND n.props ->> 'story_id' = any(cast(:story_ids AS text[]))
ORDER BY n.id
FOR UPDATE
