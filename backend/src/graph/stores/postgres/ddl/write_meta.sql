-- Record one value of the graph schema's meta table unless it is there (unscoped). Parameters: key, value.
INSERT INTO graph.meta (key, value)
VALUES (:key, :value)
ON CONFLICT (key) DO NOTHING
