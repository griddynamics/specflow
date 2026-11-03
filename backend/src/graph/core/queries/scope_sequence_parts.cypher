// Pieces of the __PATTERN__ and __CONDITIONS__ slots of scope_sequence.cypher (graph.stores._bolt.cypher puts them
// together, one hop per relation of the sequence): __START__ opens the pattern, __HOP__ is the relationship of hop
// __INDEX__ (its arrow from syntax_parts.cypher), __NODE__ the spec node a hop reaches before the last one and
// __ROOT__ the end of the last hop; __HOP_CONDITION__ and __NODE_CONDITION__ are their conditions.
MATCH (root:SpecNode {group_id: $project_id, spec_id: $spec_id})
MATCH /*__START__*/(n:SpecNode)/*__START__*//*__HOP__*/-[r__INDEX__:__TYPE__]/*__HOP__*/->/*__NODE__*/(x__INDEX__:SpecNode)/*__NODE__*/-[e:__TYPE__]->/*__ROOT__*/(root)/*__ROOT__*/
WHERE /*__HOP_CONDITION__*/r__INDEX__.group_id = $project_id/*__HOP_CONDITION__*/ AND /*__NODE_CONDITION__*/x__INDEX__.group_id = $project_id AND none(label IN labels(x__INDEX__) WHERE label IN $roots)/*__NODE_CONDITION__*/ AND e.group_id = $project_id
RETURN DISTINCT n.uuid AS uuid
