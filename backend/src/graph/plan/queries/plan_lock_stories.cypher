// Story calls: lock the Story nodes of a claim unit in the order given (the caller sorts them by story id), then
// answer their stored state, the claim token included, as it is after any concurrent change commits.
UNWIND $story_ids AS story_id
MATCH (s:Story {group_id: $project_id, story_id: story_id})
SET s._lock = true
REMOVE s._lock
RETURN s.story_id AS story_id, properties(s) AS story
ORDER BY story_id
