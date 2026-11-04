// The (group_id, spec_id) lookup index; the uniqueness is the derived spec_key (see schema.py).
CREATE INDEX graph_spec_node_spec_id IF NOT EXISTS FOR (n:SpecNode) ON (n.group_id, n.spec_id)
