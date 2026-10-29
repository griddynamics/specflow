// Fixture (positive): `Person` is a banned scan label, but `Person.name` carries a range
// index, so this anchor must plan as an index seek, not a NodeByLabelScan.
MATCH (p:Person {name: $name})
RETURN p.id AS id
