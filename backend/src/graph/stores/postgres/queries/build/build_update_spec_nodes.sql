-- Persist: update the properties of existing spec nodes in a batch (SET += : a
-- null removes the property; the embedding goes to the vector column).
-- Parameter rows: a JSON list of uuid and props. Rows of one uuid are folded
-- in order. written counts the rows that found their node.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'uuid' AS uuid,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

folded AS (
    SELECT
        r.uuid,
        graph.props_fold(array_agg(r.props - 'uuid' ORDER BY r.ord)) AS patch
    FROM r
    GROUP BY r.uuid
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(node.props, f.patch - 'embedding'),
        embedding = CASE
            WHEN
                jsonb_exists(f.patch, 'embedding')
                THEN cast(f.patch ->> 'embedding' AS vector)
            ELSE node.embedding
        END
    FROM folded AS f
    WHERE
        node.group_id = :project_id
        AND node.kind = 'spec'
        AND node.uuid = f.uuid
    RETURNING node.uuid
)

SELECT count(*) AS written
FROM r
WHERE
    r.uuid IN (
        SELECT n.uuid FROM graph.node AS n
        WHERE n.group_id = :project_id AND n.kind = 'spec'
    )
