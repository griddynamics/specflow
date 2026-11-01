-- One value of the graph schema's meta table (unscoped). Parameter: key.
SELECT m.value
FROM graph.meta AS m
WHERE m.key = :key
