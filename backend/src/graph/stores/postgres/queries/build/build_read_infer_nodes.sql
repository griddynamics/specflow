-- Infer: the nodes a judgement shows, with up to :per_node grounding fragments
-- in document order. Parameters: uuids, labels, statuses (text[]), per_node
-- (int).
WITH picked AS (
    SELECT
        n.id,
        n.uuid,
        n.spec_id,
        n.labels,
        n.props
    FROM graph.node AS n
    INNER JOIN
        unnest(cast(:uuids AS text[])) AS w (node_uuid)
        ON n.uuid = w.node_uuid
    WHERE n.group_id = :project_id AND n.kind = 'spec'
),

grounding AS (
    SELECT
        p.id AS node_id,
        f.fragment_id,
        coalesce(f.props ->> 'content_hash', '') AS content_hash,
        f.props ->> 'document_id' AS document_id,
        f.props -> 'ordinal' AS ordinal
    FROM picked AS p
    INNER JOIN
        graph.edge AS e
        ON p.id = e.source_id AND e.rel_type = 'REFERENCED_BY'
    INNER JOIN graph.node AS f ON e.target_id = f.id
    WHERE
        f.kind = 'fragment'
        AND f.group_id = :project_id
        AND f.fragment_id IS NOT NULL
        AND f.props ->> 'text' IS NOT NULL
        AND f.props ->> 'status' = any(cast(:statuses AS text[]))
),

ranked AS (
    SELECT
        node_id,
        fragment_id,
        content_hash,
        row_number() OVER (
            PARTITION BY node_id
            ORDER BY document_id COLLATE "C", ordinal, fragment_id COLLATE "C"
        ) AS rank_position
    FROM grounding
),

listed AS (
    SELECT
        node_id,
        jsonb_agg(
            jsonb_build_object(
                'fragment_id', fragment_id, 'content_hash', content_hash
            )
            ORDER BY rank_position
        )
            AS fragments
    FROM ranked
    WHERE rank_position <= :per_node
    GROUP BY node_id
),

found AS (
    SELECT
        p.uuid,
        p.spec_id,
        coalesce(p.props ->> 'name', '') AS name,
        p.props -> 'identity_key' AS identity_key,
        array(
            SELECT l FROM unnest(p.labels) AS l
            WHERE l = any(cast(:labels AS text[]))
        ) AS labels,
        p.props -> 'statements' AS statements,
        coalesce(l.fragments, cast('[]' AS jsonb)) AS fragments
    FROM picked AS p
    LEFT JOIN listed AS l ON p.id = l.node_id
)

SELECT
    uuid,
    spec_id,
    name,
    identity_key,
    labels,
    statements,
    fragments
FROM found
ORDER BY uuid COLLATE "C"
