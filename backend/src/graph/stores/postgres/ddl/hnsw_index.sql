-- The HNSW index of one embedding model: a partial halfvec index, dimension
-- at most 4000 (unscoped). A query orders by the same cast and filters on the
-- same model, so the planner matches the index.
-- The slots INDEX, DIM and MODEL hold the index name (with a hash of the
-- model), the dimension and the model as a quoted literal. The adapter
-- renders them (render_query) from validated values.
CREATE INDEX IF NOT EXISTS /*__INDEX__*/node_emb_hnsw_default/*__INDEX__*/
ON graph.node
USING hnsw (
    (CAST(embedding AS halfvec(/*__DIM__*/3072/*__DIM__*/))) halfvec_cosine_ops
)
WHERE embedding_model = /*__MODEL__*/'m-3072'/*__MODEL__*/
