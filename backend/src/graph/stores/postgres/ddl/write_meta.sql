-- Record a value in the graph schema's meta table unless it is there
-- (unscoped). Parameters: key, value.
INSERT INTO graph.meta (meta_key, meta_value)
VALUES (:key, :value)
ON CONFLICT (meta_key) DO NOTHING
