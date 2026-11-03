-- Infer (attach_orphan): for each node of :uuids, the :limit most similar
-- target nodes by exact embedding cosine, leaving out those of :excluded and
-- those it already has an edge of the relation with; best first, ties by
-- spec_id, then uuid.
-- Parameters: rel_type, uuids, target_labels, excluded, bases (text[]),
-- limit. The slot SYMMETRIC is a boolean literal (TRUE: the relation counts
-- either way; default FALSE). Score = (1 + cos) / 2 = (2 - distance) / 2. A vector of
-- another size or a zero vector (NaN distance) never matches; the size is
-- compared inside CASE so the planner cannot call <=> on mixed sizes. Rows:
-- uuid and candidates (a JSON list of uuid and score); ordered by uuid.
WITH subjects AS (
    SELECT
        s.id,
        s.uuid,
        s.embedding
    FROM graph.node AS s
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(cast(:uuids AS text[]))
        AND s.embedding IS NOT NULL
),

scored AS (
    SELECT
        s.uuid AS subject_uuid,
        c.uuid AS candidate_uuid,
        c.spec_id AS candidate_spec_id,
        CASE
            WHEN vector_dims(c.embedding) = vector_dims(s.embedding)
                THEN (2 - (c.embedding <=> s.embedding)) / 2
        END AS score
    FROM subjects AS s
    INNER JOIN graph.node AS c
        ON
            c.group_id = :project_id
            AND c.kind = 'spec'
            AND c.uuid IS NOT NULL
            AND s.id <> c.id
            AND c.labels && cast(:target_labels AS text[])
            AND c.embedding IS NOT NULL
    WHERE
        NOT c.uuid = any(cast(:excluded AS text[]))
        AND NOT EXISTS (
            SELECT 1
            FROM graph.edge AS x
            WHERE
                x.group_id = :project_id
                AND x.rel_type = :rel_type
                AND coalesce(x.props ->> 'basis', 'stated')
                = any(cast(:bases AS text[]))
                AND (
                    (x.source_id = s.id AND x.target_id = c.id)
                    OR (
                        /*__SYMMETRIC__*/FALSE/*__SYMMETRIC__*/
                        AND x.source_id = c.id
                        AND x.target_id = s.id
                    )
                )
        )
),

ranked AS (
    SELECT
        subject_uuid,
        candidate_uuid,
        score,
        row_number() OVER (
            PARTITION BY subject_uuid
            ORDER BY
                score DESC,
                candidate_spec_id COLLATE "C",
                candidate_uuid COLLATE "C"
        ) AS rank_position
    FROM scored
    WHERE score IS NOT NULL AND score <> cast('NaN' AS float8)
),

best AS (
    SELECT
        subject_uuid,
        jsonb_agg(
            jsonb_build_object('uuid', candidate_uuid, 'score', score)
            ORDER BY rank_position
        ) AS candidates
    FROM ranked
    WHERE rank_position <= :limit
    GROUP BY subject_uuid
),

found AS (
    SELECT
        s.uuid,
        coalesce(best.candidates, cast('[]' AS jsonb)) AS candidates
    FROM subjects AS s
    LEFT JOIN best ON s.uuid = best.subject_uuid
)

SELECT
    uuid,
    candidates
FROM found
ORDER BY uuid COLLATE "C"
