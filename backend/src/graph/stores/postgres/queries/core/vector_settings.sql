-- Session settings of a vector query, local to the transaction it runs in
-- (not a template: the dialect puts it before the vector statement).
-- plan_cache_mode keeps a prepared statement from falling back to the generic
-- plan, which cannot use the partial HNSW index (spike section 4); the hnsw
-- settings let the index keep scanning while the project filter drops rows.
-- Parameters: plan_cache_mode, iterative_scan, ef_search, max_scan_tuples.
SELECT
    set_config('plan_cache_mode', :plan_cache_mode, TRUE) AS plan_cache_mode,
    set_config('hnsw.iterative_scan', :iterative_scan, TRUE) AS iterative_scan,
    set_config('hnsw.ef_search', :ef_search, TRUE) AS ef_search,
    set_config('hnsw.max_scan_tuples', :max_scan_tuples, TRUE)
        AS max_scan_tuples
