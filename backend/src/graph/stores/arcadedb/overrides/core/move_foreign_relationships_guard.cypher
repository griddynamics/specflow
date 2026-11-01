// ArcadeDB override of core/queries/move_foreign_relationships.cypher, last statement: counts the relationships
// of the old node that build does not own and that no per-type statement carried over (a type whose name
// cannot be rendered). The plan's reducer refuses the move when there is one (the transaction rolls back).
MATCH (old:SpecNode {group_id: $project_id, uuid: $from_key})
WHERE $from_label IN labels(old)
MATCH (new:SpecNode {group_id: $project_id, uuid: $to_key})
WHERE $to_label IN labels(new) AND new <> old
MATCH (old)-[x]-()
WHERE NOT type(x) IN $owned_types
RETURN count(x) AS left
