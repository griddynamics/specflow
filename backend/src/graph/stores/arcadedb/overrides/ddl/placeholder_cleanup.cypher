// Deletes every placeholder spec node left by an interrupted schema setup (see placeholder_create.cypher).
MATCH (n:SpecNode {group_id: $group})
DETACH DELETE n
