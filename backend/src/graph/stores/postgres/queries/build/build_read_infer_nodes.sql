-- Infer: the nodes a judgement shows, with up to :per_node grounding fragments
-- in document order. Each fragment has its heading path and, from its text,
-- the heading and code fence lines up to the node's first evidence line in it
-- (headings; none when the evidence edge or the fragment has no line), so the
-- section the node sits in is found in code; document_id groups batched
-- orphans by document. quote is the node's :quote attribute (the judgement
-- input hash keys a node by identity key, label, name and quote, never its
-- reworded texts). Parameters: uuids, labels, statuses (text[]), per_node
-- (int), quote (text). A heading line is what the Cypher pattern
-- ' {0,3}(#{1,6}([ \t].*)?|```.*|~~~.*)' matches as a whole (a dot never
-- matches a line terminator, as in Java).
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
        f.props -> 'ordinal' AS ordinal,
        coalesce(f.props -> 'heading_path', cast('[]' AS jsonb)) AS heading_path,
        f.props ->> 'text' AS fragment_text,
        cast(f.props ->> 'line_start' AS integer) AS fragment_line,
        min(cast(e.props ->> 'line_start' AS integer)) AS line
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
    GROUP BY p.id, f.id, f.fragment_id, f.props
),

ranked AS (
    SELECT
        node_id,
        fragment_id,
        content_hash,
        document_id,
        heading_path,
        CASE
            WHEN line IS NULL OR fragment_line IS NULL OR line < fragment_line
                THEN cast(ARRAY[] AS text[])
            ELSE
                (string_to_array(fragment_text, chr(10)))[
                    1:(line - fragment_line + 1)
                ]
        END AS above,
        row_number() OVER (
            PARTITION BY node_id
            ORDER BY document_id COLLATE "C", ordinal, fragment_id COLLATE "C"
        ) AS rank_position
    FROM grounding
),

listed AS (
    SELECT
        r.node_id,
        jsonb_agg(
            jsonb_build_object(
                'fragment_id', r.fragment_id,
                'document_id', r.document_id,
                'content_hash', r.content_hash,
                'heading_path', r.heading_path,
                'headings', (
                    SELECT coalesce(jsonb_agg(h.text_line ORDER BY h.position), cast('[]' AS jsonb))
                    FROM unnest(r.above) WITH ORDINALITY AS h (text_line, position)
                    WHERE
                        h.text_line
                        ~ '^ {0,3}(#{1,6}([ \t][^\n\r\u0085  ]*)?|```[^\n\r\u0085  ]*|~~~[^\n\r\u0085  ]*)$'
                )
            )
            ORDER BY r.rank_position
        )
            AS fragments
    FROM ranked AS r
    WHERE r.rank_position <= :per_node
    GROUP BY r.node_id
),

found AS (
    SELECT
        p.uuid,
        p.spec_id,
        coalesce(p.props ->> 'name', '') AS name,
        p.props -> 'identity_key' AS identity_key,
        coalesce(p.props -> cast(:quote AS text), to_jsonb(cast('' AS text))) AS quote,
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
    quote,
    labels,
    statements,
    fragments
FROM found
ORDER BY uuid COLLATE "C"
