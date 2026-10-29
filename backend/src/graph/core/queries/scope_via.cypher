// Scope: nodes reaching the root by __MIN_HOPS__..__MAX_HOPS__ edges of any of the relation types
// (__MIN_HOPS__ and __MAX_HOPS__ are slots for the hop counts, default 1 and 3). The literal upper
// bound is the walk's hard cap; $max_hops, the spine path's `max` (graph.core.reader), is its
// effective bound (policy.capped_traversals in graph-schema.yaml).
MATCH (root:SpecNode {group_id: $project_id, spec_id: $spec_id})
// The bounded walk: only edges of the project are expanded (pruned inside the pattern, so the plan
// is one variable-length expand capped at the literal bound). Every edge is followed in its stored
// direction towards the root unless the relation is in $symmetric (hop i leaves nodes(p)[i]), and
// the walk never passes through a node with a spine-root label ($roots).
MATCH p = (n:SpecNode)(()-[r:__TYPES__ WHERE r.group_id = $project_id]-()){/*__MIN_HOPS__*/1/*__MIN_HOPS__*/,/*__MAX_HOPS__*/3/*__MAX_HOPS__*/}(root)
WHERE length(p) <= $max_hops AND n <> root AND n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels)
  AND all(i IN range(0, size(r) - 1) WHERE type(r[i]) IN $symmetric OR startNode(r[i]) = nodes(p)[i])
  AND all(b IN nodes(p)[1..] WHERE b = root OR none(label IN labels(b) WHERE label IN $roots))
RETURN DISTINCT n.uuid AS uuid
