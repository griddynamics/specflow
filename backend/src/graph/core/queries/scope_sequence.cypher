// Scope: nodes reaching the root by one edge of each relation of a sequence, in order.
// __PATTERN__ and __CONDITIONS__ are slots rendered from the validated relation names; their default
// is a sequence of one directed relation.
MATCH (root:SpecNode {group_id: $project_id, spec_id: $spec_id})
MATCH /*__PATTERN__*/(n:SpecNode)-[r0:__TYPE__]->(root)/*__PATTERN__*/
WHERE /*__CONDITIONS__*/r0.group_id = $project_id/*__CONDITIONS__*/ AND n <> root AND n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels)
RETURN DISTINCT n.uuid AS uuid
