-- Persist: create or update documents in a batch (SET += : a null removes a
-- property). Parameters: project_id, build_id, rows: a JSON list of
-- document_id and props. Rows of one document are folded in order. A document
-- that exists is updated, a missing one inserted. written counts the input
-- rows.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'document_id' AS document_id,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

folded AS (
    SELECT
        r.document_id,
        graph.props_fold(array_agg(r.props - 'document_id' ORDER BY r.ord))
        || jsonb_build_object('last_build_id', cast(:build_id AS text)) AS patch
    FROM r
    GROUP BY r.document_id
),

updated AS (
    UPDATE graph.node
    SET props = graph.props_merge(node.props, f.patch)
    FROM folded AS f
    WHERE
        node.group_id = :project_id
        AND node.kind = 'document'
        AND node.document_id = f.document_id
    RETURNING node.document_id
),

inserted AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    SELECT
        :project_id AS group_id,
        'document' AS kind,
        ARRAY['Document'] AS labels,
        graph.props_merge(
            f.patch, jsonb_build_object('document_id', f.document_id)
        ) AS props
    FROM folded AS f
    WHERE f.document_id NOT IN (SELECT u.document_id FROM updated AS u)
    ON CONFLICT (group_id, document_id) WHERE kind = 'document' DO UPDATE
        SET
            props = graph.props_merge(
                node.props,
                (
                    SELECT g.patch
                    FROM folded AS g
                    WHERE g.document_id = excluded.props ->> 'document_id'
                )
            )
    RETURNING node.document_id
)

SELECT count(*) AS written FROM r
