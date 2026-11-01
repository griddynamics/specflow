-- Diff: each node's REFERENCED_BY (with its evidence line text) and
-- MENTIONED_IN links, one branch per type, each reading only the properties
-- its writer sets: lines on REFERENCED_BY, count on MENTIONED_IN; the other
-- columns are null. The lines are the fragment text cut at the evidence lines
-- (line i of the text is source line line_start + i).
WITH links AS (
    SELECT
        n.uuid,
        r.rel_type AS kind,
        f.fragment_id,
        coalesce(f.props ->> 'document_id', '') AS document_id,
        r.props -> 'line_start' AS line_start,
        r.props -> 'line_end' AS line_end,
        cast(NULL AS jsonb) AS link_count,
        CASE
            WHEN
                f.props ->> 'text' IS NOT NULL
                AND f.props ->> 'line_start' IS NOT NULL
                AND r.props ->> 'line_start' IS NOT NULL
                AND r.props ->> 'line_end' IS NOT NULL
                THEN
                    (string_to_array(f.props ->> 'text', E'\n'))[
                        cast(r.props ->> 'line_start' AS integer)
                        - cast(f.props ->> 'line_start' AS integer)
                        + 1:
                        cast(r.props ->> 'line_end' AS integer)
                        - cast(f.props ->> 'line_start' AS integer)
                        + 1
                    ]
            ELSE cast(ARRAY[] AS text[])
        END AS lines
    FROM graph.node AS n
    INNER JOIN
        graph.edge AS r
        ON n.id = r.source_id AND r.rel_type = 'REFERENCED_BY'
    INNER JOIN graph.node AS f ON r.target_id = f.id AND f.kind = 'fragment'
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid IS NOT NULL
        AND f.group_id = :project_id
    UNION ALL
    SELECT
        n.uuid,
        m.rel_type AS kind,
        f.fragment_id,
        coalesce(f.props ->> 'document_id', '') AS document_id,
        cast(NULL AS jsonb) AS line_start,
        cast(NULL AS jsonb) AS line_end,
        m.props -> 'count' AS link_count,
        cast(ARRAY[] AS text[]) AS lines
    FROM graph.node AS n
    INNER JOIN
        graph.edge AS m
        ON n.id = m.source_id AND m.rel_type = 'MENTIONED_IN'
    INNER JOIN graph.node AS f ON m.target_id = f.id AND f.kind = 'fragment'
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid IS NOT NULL
        AND f.group_id = :project_id
)

SELECT
    uuid,
    kind,
    fragment_id,
    document_id,
    line_start,
    line_end,
    link_count AS count,
    lines
FROM links
ORDER BY uuid COLLATE "C", kind COLLATE "C", fragment_id COLLATE "C", line_start
