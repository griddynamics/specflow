// Known gap: neither CyVer (no `var.prop` access to check) nor the plan check (SET may create
// any property key) catches an undeclared key written through `SET var += {...}`. The static
// lint's cypher-lint-set-unknown-property catches it.
MATCH (p:Person)
SET p += {not_a_declared_property: 1}
