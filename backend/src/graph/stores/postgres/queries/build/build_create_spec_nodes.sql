-- Persist: create spec nodes in a batch; the domain label is set on creation
-- only. Parameter rows: a JSON list of uuid, label and props. Rows with the
-- same uuid are folded in order first (the first label wins, the props merge
-- left to right, a null removes a key), as MERGE does row by row. A node that
-- exists is updated, a missing one inserted (the unique key stays the safety
-- net). The embedding goes to the vector column; written is the number of
-- input rows.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'uuid' AS uuid,
        e.item ->> 'label' AS node_label,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

folded AS (
    SELECT
        r.uuid,
        (array_agg(r.node_label ORDER BY r.ord))[1] AS node_label,
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
),

inserted AS (
    INSERT INTO graph.node (group_id, kind, labels, props, embedding)
    SELECT
        :project_id AS group_id,
        'spec' AS kind,
        ARRAY[f.node_label] AS labels,
        graph.props_merge(
            f.patch - 'embedding', jsonb_build_object('uuid', f.uuid)
        ) AS props,
        CASE
            WHEN
                jsonb_exists(f.patch, 'embedding')
                THEN cast(f.patch ->> 'embedding' AS vector)
        END AS embedding
    FROM folded AS f
    WHERE f.uuid NOT IN (SELECT u.uuid FROM updated AS u)
    ON CONFLICT (group_id, uuid) WHERE kind = 'spec' DO NOTHING
    RETURNING node.uuid
)

SELECT count(*) AS written FROM r
