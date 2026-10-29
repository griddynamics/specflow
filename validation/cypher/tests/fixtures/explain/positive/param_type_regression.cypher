// Fixture (positive): a list parameter in an `IN` predicate and an integer `LIMIT`; both
// start bound to a string and must be retyped from the server's own type error.
MATCH (p:Person {name: $name})
WHERE p.name IN $names
RETURN p.id AS id
LIMIT $page_size
