-- Persist: create or update fragments in a batch and link them to their
-- document. A row whose document does not exist writes nothing. Parameters:
-- project_id, build_id, rows: a JSON list of fragment_id, document_id and
-- props. Rows of one fragment are folded in order. written counts the rows
-- whose document exists. A fragment that a concurrent writer inserted first is
-- updated (ON CONFLICT) and not linked again: that writer linked it.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'fragment_id' AS fragment_id,
        e.item ->> 'document_id' AS document_id,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

linked AS (
    SELECT
        r.fragment_id,
        r.props,
        r.ord,
        d.id AS document_node
    FROM r
    INNER JOIN graph.node AS d
        ON
            d.group_id = :project_id
            AND d.kind = 'document'
            AND r.document_id = d.document_id
),

folded AS (
    SELECT
        l.fragment_id,
        (array_agg(l.document_node ORDER BY l.ord))[1] AS document_node,
        graph.props_fold(array_agg(l.props - 'fragment_id' ORDER BY l.ord))
        || jsonb_build_object('last_build_id', cast(:build_id AS text)) AS patch
    FROM linked AS l
    GROUP BY l.fragment_id
),

updated AS (
    UPDATE graph.node
    SET props = graph.props_merge(node.props, f.patch)
    FROM folded AS f
    WHERE
        node.group_id = :project_id
        AND node.kind = 'fragment'
        AND node.fragment_id = f.fragment_id
    RETURNING node.id, node.fragment_id
),

inserted AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    SELECT
        :project_id AS group_id,
        'fragment' AS kind,
        ARRAY['Fragment'] AS labels,
        graph.props_merge(
            f.patch, jsonb_build_object('fragment_id', f.fragment_id)
        ) AS props
    FROM folded AS f
    WHERE f.fragment_id NOT IN (SELECT u.fragment_id FROM updated AS u)
    ON CONFLICT (group_id, fragment_id) WHERE kind = 'fragment' DO UPDATE
        SET
            props = graph.props_merge(
                node.props,
                (
                    SELECT g.patch
                    FROM folded AS g
                    WHERE g.fragment_id = excluded.props ->> 'fragment_id'
                )
            )
    RETURNING node.id, node.fragment_id, (node.xmax = 0) AS fresh
),

written AS (
    SELECT
        u.id,
        u.fragment_id
    FROM updated AS u
    UNION ALL
    SELECT
        i.id,
        i.fragment_id
    FROM inserted AS i
    WHERE i.fresh
),

contains_edges AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'CONTAINS' AS rel_type,
        f.document_node AS source_id,
        w.id AS target_id,
        cast('{}' AS jsonb) AS props
    FROM written AS w
    INNER JOIN folded AS f ON w.fragment_id = f.fragment_id
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.edge AS x
            WHERE
                x.rel_type = 'CONTAINS'
                AND x.source_id = f.document_node
                AND x.target_id = w.id
        )
    RETURNING edge.id
)

SELECT count(*) AS written FROM linked
