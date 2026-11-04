// Pieces of the __PATTERN__ slot of infer_realization_subjects.cypher (graph.stores._bolt.cypher): a sequence path
// is one __STEP__ per relation (its arrow from core/queries/syntax_parts.cypher), the steps joined by __THROUGH__;
// a via path is one __RANGE__ over its relations (joined by the __OR__ of syntax_parts.cypher) and its hop bounds.
MATCH (m:SpecNode {group_id: $project_id})/*__STEP__*/-[:__TYPE__]/*__STEP__*/->/*__THROUGH__*/(:SpecNode)/*__THROUGH__*//*__RANGE__*/-[:__TYPES__*/*__MIN_HOPS__*/1/*__MIN_HOPS__*/../*__MAX_HOPS__*/3/*__MAX_HOPS__*/]/*__RANGE__*/->(c:SpecNode)
WHERE c.group_id = $project_id
RETURN c.uuid AS uuid
