-- BM25 replacement of core/queries/search_fragments.cypher over
-- Fragment(text). Parameters: project_id, terms (text[]), active (status
-- value), document_id (nullable text), skip, page. The slots BM25_K and BM25_B
-- hold the constants as in search_nodes_fulltext. One row: fetched and hits
-- (jsonb list of fragment_id and score). The statistics cover all fragments
-- of the project (any status), as the Lucene index covers every fragment.
WITH q AS (
    SELECT DISTINCT u.lexeme
    FROM unnest(
        to_tsvector(
            'simple',
            graph.fts_norm(array_to_string(cast(:terms AS text[]), ' '))
        )
    ) AS u
),

corpus AS (
    SELECT
        n.id,
        n.fragment_id,
        n.props,
        n.text_tsv,
        n.text_len
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'fragment'
),

fs AS (
    SELECT
        count(*) FILTER (WHERE text_len > 0) AS n_docs,
        avg(text_len) FILTER (WHERE text_len > 0) AS avgdl
    FROM corpus
),

tf AS (
    SELECT
        c.id,
        u.lexeme,
        c.text_len AS dl,
        cardinality(u.positions) AS tf
    FROM corpus AS c
    CROSS JOIN LATERAL unnest(c.text_tsv) AS u
    WHERE u.lexeme IN (SELECT q.lexeme FROM q)
),

df AS (
    SELECT
        lexeme,
        count(*) AS df
    FROM tf
    GROUP BY lexeme
),

scored AS (
    SELECT
        t.id,
        sum(
            ln(1 + (fs.n_docs - d.df + 0.5) / (d.df + 0.5))
            * t.tf / (
                t.tf + /*__BM25_K__*/1.2/*__BM25_K__*/ * (
                    1 - /*__BM25_B__*/0.75/*__BM25_B__*/
                    + /*__BM25_B__*/0.75/*__BM25_B__*/ * t.dl / fs.avgdl
                )
            )
        ) AS score
    FROM tf AS t
    INNER JOIN df AS d ON t.lexeme = d.lexeme
    CROSS JOIN fs
    GROUP BY t.id
),

hits AS (
    SELECT
        c.fragment_id,
        sc.score
    FROM scored AS sc
    INNER JOIN corpus AS c ON sc.id = c.id
    WHERE
        c.fragment_id IS NOT NULL
        AND c.props ->> 'status' = :active
        AND (
            cast(:document_id AS text) IS NULL
            OR c.props ->> 'document_id' = cast(:document_id AS text)
        )
),

page AS (
    SELECT
        fragment_id,
        score,
        row_number() OVER (
            ORDER BY score DESC, fragment_id COLLATE "C"
        ) AS rn
    FROM hits
    ORDER BY score DESC, fragment_id COLLATE "C"
    LIMIT :page OFFSET :skip
)

SELECT
    count(*) AS fetched,
    coalesce(
        jsonb_agg(
            jsonb_build_object('fragment_id', fragment_id, 'score', score)
            ORDER BY rn
        ),
        cast('[]' AS jsonb)
    ) AS hits
FROM page
