// Fixture (positive): a plain MATCH over a declared path -- scores 1.0 with no metadata.
MATCH (p:Person)-[:ACTED_IN]->(m:Movie)
RETURN p, m
