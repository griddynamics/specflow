// Known gap: CyVer's properties validator reads `var.prop` dot access and inline maps
// only, never a map projection `var {.prop}`, so this unknown key passes it. The plan check
// catches it: EXPLAIN warns about a property key no node has ever carried.
MATCH (p:Person)
RETURN p {.id, .not_a_declared_property}
