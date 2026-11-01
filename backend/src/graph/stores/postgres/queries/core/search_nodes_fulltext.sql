-- BM25 replacement of core/queries/search_nodes_fulltext.cypher (Lucene
-- BM25Similarity per field, summed over name and statement_text).
-- Parameters: project_id, terms (text[], OR semantics), labels, where
-- (jsonb), skip, page. The slots BM25_K (1.2) and BM25_B (0.75) hold the
-- constants. One row: fetched (rows of this page, already filtered by
-- project, label and where) and hits (jsonb list of uuid, spec_id and score)
-- in score order, ties by spec_id then uuid; the reader's loop (skip +=
-- fetched, page *= 2, stop on fetched < page) works unchanged.
-- idf = ln(1 + (N - df + 0.5) / (df + 0.5)); a term scores
-- idf * tf / (tf + k1 * (1 - b + b * dl / avgdl)). N, df and avgdl are per
-- project and per field over the documents that have the field. The terms
-- are tokenised as the documents are (fts_norm, 'simple'); duplicates
-- collapse.
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
        n.uuid,
        n.spec_id,
        n.labels,
        n.props,
        n.name_tsv,
        n.name_len,
        n.stmt_tsv,
        n.stmt_len
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec'
),

fs AS (
    SELECT
        count(*) FILTER (WHERE name_len > 0) AS n_name,
        avg(name_len) FILTER (WHERE name_len > 0) AS avg_name,
        count(*) FILTER (WHERE stmt_len > 0) AS n_stmt,
        avg(stmt_len) FILTER (WHERE stmt_len > 0) AS avg_stmt
    FROM corpus
),

name_tf AS (
    SELECT
        c.id,
        u.lexeme,
        graph.lucene_len(c.name_len) AS dl,
        cardinality(u.positions) AS tf
    FROM corpus AS c
    CROSS JOIN LATERAL unnest(c.name_tsv) AS u
    WHERE u.lexeme IN (SELECT q.lexeme FROM q)
),

stmt_tf AS (
    SELECT
        c.id,
        u.lexeme,
        graph.lucene_len(c.stmt_len) AS dl,
        cardinality(u.positions) AS tf
    FROM corpus AS c
    CROSS JOIN LATERAL unnest(c.stmt_tsv) AS u
    WHERE u.lexeme IN (SELECT q.lexeme FROM q)
),

name_df AS (
    SELECT
        lexeme,
        count(*) AS df
    FROM name_tf
    GROUP BY lexeme
),

stmt_df AS (
    SELECT
        lexeme,
        count(*) AS df
    FROM stmt_tf
    GROUP BY lexeme
),

contrib AS (
    SELECT
        t.id,
        ln(1 + (fs.n_name - d.df + 0.5) / (d.df + 0.5))
        * t.tf / (
            t.tf + /*__BM25_K__*/1.2/*__BM25_K__*/ * (
                1 - /*__BM25_B__*/0.75/*__BM25_B__*/
                + /*__BM25_B__*/0.75/*__BM25_B__*/ * t.dl / fs.avg_name
            )
        ) AS s
    FROM name_tf AS t
    INNER JOIN name_df AS d ON t.lexeme = d.lexeme
    CROSS JOIN fs
    UNION ALL
    SELECT
        t.id,
        ln(1 + (fs.n_stmt - d.df + 0.5) / (d.df + 0.5))
        * t.tf / (
            t.tf + /*__BM25_K__*/1.2/*__BM25_K__*/ * (
                1 - /*__BM25_B__*/0.75/*__BM25_B__*/
                + /*__BM25_B__*/0.75/*__BM25_B__*/ * t.dl / fs.avg_stmt
            )
        ) AS s
    FROM stmt_tf AS t
    INNER JOIN stmt_df AS d ON t.lexeme = d.lexeme
    CROSS JOIN fs
),

scored AS (
    SELECT
        id,
        sum(s) AS score
    FROM contrib
    GROUP BY id
),

hits AS (
    SELECT
        c.uuid,
        c.spec_id,
        sc.score
    FROM scored AS sc
    INNER JOIN corpus AS c ON sc.id = c.id
    WHERE
        c.spec_id IS NOT NULL
        AND c.labels && cast(:labels AS text[])
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_each(cast(:where AS jsonb)) AS w
            WHERE NOT (
                coalesce(c.props -> w.key = w.value, FALSE)
                OR (
                    c.props -> w.key IS NULL
                    AND w.value = cast('null' AS jsonb)
                )
            )
        )
),

page AS (
    SELECT
        uuid,
        spec_id,
        score,
        row_number() OVER (
            ORDER BY score DESC, spec_id COLLATE "C", uuid COLLATE "C"
        ) AS rn
    FROM hits
    ORDER BY score DESC, spec_id COLLATE "C", uuid COLLATE "C"
    LIMIT :page OFFSET :skip
)

SELECT
    count(*) AS fetched,
    coalesce(
        jsonb_agg(
            jsonb_build_object('uuid', uuid, 'spec_id', spec_id, 'score', score)
            ORDER BY rn
        ),
        cast('[]' AS jsonb)
    ) AS hits
FROM page
