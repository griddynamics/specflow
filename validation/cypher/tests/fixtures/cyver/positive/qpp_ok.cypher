// Fixture (positive): a quantified path pattern over a declared type.
MATCH p = (start:Person {id: $person_id}) ((:Person)-[:FOLLOWS]->(:Person)){1,3}
RETURN p
