// Fixture (negative): two disconnected patterns in one MATCH -- a cartesian product.
MATCH (a:Person {name: $first}), (b:Person {name: $second})
RETURN a.id AS first, b.id AS second
