// Fixture (negative): MERGE of an unknown relationship type.
MATCH (a:Person {id: $first_id}), (b:Person {id: $second_id})
MERGE (a)-[:NOT_A_REL]->(b)
