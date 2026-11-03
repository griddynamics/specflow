// Syntax pieces of the Cypher dialect (graph.stores._bolt.cypher) as slots, so its renderers write no query text:
// __DIRECTED__ and __EITHER__ are the arrows of a directed and of a symmetric relation, __OR__ separates the
// relationship types of one pattern, __AND__ separates conditions and __UNION__ separates subquery branches.
MATCH (a:SpecNode {group_id: $project_id})
CALL (a) {
MATCH (s:SpecNode {group_id: $project_id})-[r:__TYPE__/*__OR__*/|/*__OR__*/__TYPE__]/*__DIRECTED__*/->/*__DIRECTED__*/(b:SpecNode)
WHERE s = a AND b.group_id = $project_id/*__AND__*/ AND /*__AND__*/r.group_id = $project_id
RETURN b.uuid AS uuid
/*__UNION__*/UNION/*__UNION__*/
MATCH (t:SpecNode {group_id: $project_id})-[q:__TYPE__]/*__EITHER__*/-/*__EITHER__*/(c:SpecNode)
WHERE t = a AND c.group_id = $project_id
RETURN c.uuid AS uuid
}
RETURN uuid
