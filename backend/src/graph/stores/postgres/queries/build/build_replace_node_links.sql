-- Persist: replace the evidence and mention links of nodes in a batch. A
-- missing fragment is created as a key-only stub: fragments are written in the
-- last phase. Parameters: project_id, rows: a JSON list of uuid, evidence
-- (fragment_id, line_start, line_end) and mentions (fragment_id, count).
-- Evidence and mentions create one relationship per entry, as CREATE does.
-- written counts the rows that found their node.
WITH r AS (
    SELECT
        e.item ->> 'uuid' AS uuid,
        coalesce(e.item -> 'evidence', cast('[]' AS jsonb)) AS evidence,
        coalesce(e.item -> 'mentions', cast('[]' AS jsonb)) AS mentions
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

nodes AS (
    SELECT
        n.id,
        r.evidence,
        r.mentions
    FROM r
    INNER JOIN graph.node AS n
        ON n.group_id = :project_id AND n.kind = 'spec' AND r.uuid = n.uuid
),

removed AS (
    DELETE FROM graph.edge
    WHERE
        edge.source_id IN (SELECT nodes.id FROM nodes)
        AND edge.rel_type IN ('REFERENCED_BY', 'MENTIONED_IN')
        AND edge.target_id IN (
            SELECT f.id FROM graph.node AS f
            WHERE f.kind = 'fragment'
        )
    RETURNING edge.id
),

wanted AS (
    SELECT DISTINCT x.item ->> 'fragment_id' AS fragment_id
    FROM nodes
    CROSS JOIN LATERAL (
        SELECT a.item FROM jsonb_array_elements(nodes.evidence) AS a (item)
        UNION ALL
        SELECT b.item FROM jsonb_array_elements(nodes.mentions) AS b (item)
    ) AS x
),

fragments AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    SELECT
        :project_id AS group_id,
        'fragment' AS kind,
        ARRAY['Fragment'] AS labels,
        jsonb_build_object('fragment_id', w.fragment_id) AS props
    FROM wanted AS w
    ON CONFLICT (group_id, fragment_id) WHERE kind = 'fragment'
    DO UPDATE SET props = node.props
    RETURNING node.id, node.fragment_id
),

evidence_edges AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'REFERENCED_BY' AS rel_type,
        nodes.id AS source_id,
        f.id AS target_id,
        graph.props_merge(
            cast('{}' AS jsonb),
            jsonb_build_object(
                'line_start',
                ev.item -> 'line_start',
                'line_end',
                ev.item -> 'line_end'
            )
        ) AS props
    FROM nodes
    CROSS JOIN LATERAL jsonb_array_elements(nodes.evidence) AS ev (item)
    INNER JOIN fragments AS f ON f.fragment_id = ev.item ->> 'fragment_id'
    RETURNING edge.id
),

mention_edges AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        'MENTIONED_IN' AS rel_type,
        nodes.id AS source_id,
        f.id AS target_id,
        graph.props_merge(
            cast('{}' AS jsonb), jsonb_build_object('count', m.item -> 'count')
        ) AS props
    FROM nodes
    CROSS JOIN LATERAL jsonb_array_elements(nodes.mentions) AS m (item)
    INNER JOIN fragments AS f ON f.fragment_id = m.item ->> 'fragment_id'
    RETURNING edge.id
)

SELECT count(*) AS written FROM nodes
