// Fixture (negative): MERGE of an unknown label.
MERGE (n:NotALabel {id: $id})
RETURN n
