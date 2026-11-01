-- HNSW page, port of search_nodes_vector.cypher. Parameters: project_id,
-- model, vector, labels, where, limit, k. The slot DIM holds the dimension of
-- the partial halfvec index (at most 4000 for an index), rendered from the
-- length of :vector. The adapter runs it with SET LOCAL plan_cache_mode =
-- force_custom_plan, hnsw.iterative_scan, hnsw.ef_search and
-- hnsw.max_scan_tuples (graph.stores.postgres.dialect, vector_settings.sql):
-- without them a prepared statement falls back to a generic plan after five
-- runs, which cannot use the partial index (spike section 4).
-- The index (ensure_schema creates one per model of a large project) matches
-- the predicate embedding_model = :model and the ordering by the same cast.
-- Row: fetched (rows of the k-page) and hits (jsonb list of uuid, spec_id and
-- score) of the project, best first. relaxed_order scans approximately in
-- order, so the outer ORDER BY sorts the k rows exactly.
WITH page AS (
    SELECT
        n.uuid,
        n.spec_id,
        (
            2 - (
                cast(n.embedding AS halfvec(/*__DIM__*/3072/*__DIM__*/))
                <=> cast(
                    cast(:vector AS vector) AS halfvec(
                        /*__DIM__*/3072/*__DIM__*/
                    )
                )
            )
        ) / 2 AS score
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.embedding_model = :model
        AND vector_dims(n.embedding) = /*__DIM__*/3072/*__DIM__*/
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:labels AS text[])
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_each(cast(:where AS jsonb)) AS w
            WHERE NOT (
                coalesce(n.props -> w.key = w.value, FALSE)
                OR (
                    n.props -> w.key IS NULL
                    AND w.value = cast('null' AS jsonb)
                )
            )
        )
    ORDER BY
        cast(n.embedding AS halfvec(/*__DIM__*/3072/*__DIM__*/))
        <=> cast(
            cast(:vector AS vector) AS halfvec(/*__DIM__*/3072/*__DIM__*/)
        )
    LIMIT :k
),

counted AS (
    SELECT count(*) AS fetched FROM page
),

ranked AS (
    SELECT
        uuid,
        spec_id,
        score
    FROM page
    WHERE score <> cast('NaN' AS float8)
    ORDER BY score DESC, spec_id COLLATE "C"
    LIMIT :limit
),

listed AS (
    SELECT
        coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'uuid', uuid, 'spec_id', spec_id, 'score', score
                )
                ORDER BY score DESC, spec_id COLLATE "C"
            ),
            cast('[]' AS jsonb)
        ) AS hits
    FROM ranked
)

SELECT
    counted.fetched,
    listed.hits
FROM counted
CROSS JOIN listed
