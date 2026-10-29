// Fixture (negative): a quantified path pattern over an unknown relationship type.
MATCH p = (start:Person {id: $person_id}) ((:Person)-[:NOT_A_REL]->(:Person)){1,3}
RETURN p
