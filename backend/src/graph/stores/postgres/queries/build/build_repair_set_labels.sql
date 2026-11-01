-- Repair: set the kept domain label and label_disagreement of nodes, removing
-- their other build-owned labels. Parameter rows: a JSON list of id (text of
-- the node id), remove (labels), label and label_disagreement (null removes
-- it). written counts the rows that found their node.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'id' AS id,
        e.item -> 'remove' AS removed,
        e.item ->> 'label' AS kept_label,
        e.item -> 'label_disagreement' AS label_disagreement
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

last_row AS (
    SELECT DISTINCT ON (r.id)
        r.id,
        r.removed,
        r.kept_label,
        r.label_disagreement
    FROM r
    ORDER BY r.id ASC, r.ord DESC
),

planned AS (
    SELECT
        n.id AS node_id,
        k.label_disagreement,
        array(
            SELECT l
            FROM unnest(n.labels) AS l
            WHERE
                l <> k.kept_label
                AND NOT (
                    (
                        jsonb_typeof(k.removed) = 'array'
                        AND jsonb_exists(k.removed, l)
                    )
                    OR (
                        jsonb_typeof(k.removed) = 'string'
                        AND k.removed ->> 0 = l
                    )
                )
        ) || ARRAY[k.kept_label] AS new_labels
    FROM last_row AS k
    INNER JOIN graph.node AS n
        ON
            cast(n.id AS text) = k.id
            AND n.group_id = :project_id
            AND n.kind = 'spec'
),

updated AS (
    UPDATE graph.node
    SET
        labels = p.new_labels,
        props
        = graph.props_merge(
            node.props,
            jsonb_build_object('label_disagreement', p.label_disagreement)
        )
    FROM planned AS p
    WHERE node.id = p.node_id
    RETURNING node.id
)

SELECT count(*) AS written
FROM r
INNER JOIN graph.node AS x
    ON
        cast(x.id AS text) = r.id
        AND x.group_id = :project_id
        AND x.kind = 'spec'
