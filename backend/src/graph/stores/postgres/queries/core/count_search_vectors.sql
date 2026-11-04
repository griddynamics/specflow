-- matching: candidates whose search vector was made by :model;
-- stored: all with one.
SELECT
    count(*) FILTER (WHERE n.embedding_model = :model) AS matching,
    count(*) AS stored
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.embedding IS NOT NULL
    AND n.spec_id IS NOT NULL
    AND n.labels && cast(:labels AS text[])
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_each(cast(:where AS jsonb)) AS w
        WHERE NOT (
            coalesce(n.props -> w.key = w.value, FALSE)
            OR (
                n.props -> w.key IS NULL
                AND (
                    (
                        n.labels && array(
                            SELECT jsonb_array_elements_text(
                                coalesce(
                                    cast(:defaulted AS jsonb) -> w.key,
                                    cast('[]' AS jsonb)
                                )
                            )
                        )
                    )
                    <> (w.value = cast('null' AS jsonb))
                )
            )
        )
    )
