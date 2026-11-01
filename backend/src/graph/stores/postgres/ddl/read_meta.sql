-- One value of the graph schema's meta table (unscoped). Parameter: key.
SELECT m.meta_value
FROM graph.meta AS m
WHERE m.meta_key = :key
