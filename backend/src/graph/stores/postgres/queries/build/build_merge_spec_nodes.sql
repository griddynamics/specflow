-- Persist (merge, W3): per row, move every relationship of each merged node
-- whose type build does not own to the survivor, then delete the merged node
-- with its build-owned relationships. The survivor gets its final
-- spec_id_aliases and the build stamp. A merged node already gone is skipped.
-- Parameters: project_id, build_id, owned_types (text[]), rows: a JSON list of
-- survivor, merged (uuids) and aliases. A moved relationship keeps its type,
-- direction, other end and properties (its row is re-pointed, so edge_key and
-- id survive). It is only deleted when an equal one (or one with its edge_key)
-- already joins the survivor and the other end, when an equal one comes
-- earlier in the moved set (the sub-statements share one snapshot, so
-- duplicates inside the set are folded here), and when it joins the merged
-- node and the survivor or two nodes merged into one survivor. A self-loop of
-- a merged node becomes a self-loop of the survivor (canonical Cypher). Both
-- ends of an edge go through the old -> survivor mapping in one pass, so an
-- edge between nodes merged into different survivors ends as survivor ->
-- survivor. Moved edges that share (type, ends) and an edge_key are one (the
-- first by id), as are equal ones without a key.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'survivor' AS survivor,
        coalesce(e.item -> 'merged', cast('[]' AS jsonb)) AS merged,
        coalesce(e.item -> 'aliases', cast('[]' AS jsonb)) AS aliases
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

survivors AS (
    SELECT
        r.ord,
        r.merged,
        r.aliases,
        n.id AS new_id
    FROM r
    INNER JOIN graph.node AS n
        ON n.group_id = :project_id AND n.kind = 'spec' AND r.survivor = n.uuid
),

pairs AS (
    SELECT
        s.ord,
        s.new_id,
        o.id AS old_id
    FROM survivors AS s
    CROSS JOIN LATERAL jsonb_array_elements_text(s.merged) AS m (merged_uuid)
    INNER JOIN graph.node AS o
        ON
            o.group_id = :project_id
            AND o.kind = 'spec'
            AND m.merged_uuid = o.uuid
            AND s.new_id <> o.id
),

cand AS (
    SELECT
        e.id,
        e.rel_type,
        e.props,
        e.edge_key,
        e.source_id AS old_src,
        e.target_id AS old_tgt,
        coalesce(ps.new_id, e.source_id) AS new_src,
        coalesce(pt.new_id, e.target_id) AS new_tgt
    FROM graph.edge AS e
    LEFT JOIN pairs AS ps ON e.source_id = ps.old_id
    LEFT JOIN pairs AS pt ON e.target_id = pt.old_id
    WHERE
        e.group_id = :project_id
        AND (ps.old_id IS NOT NULL OR pt.old_id IS NOT NULL)
        AND NOT (e.rel_type = any(cast(:owned_types AS text[])))
),

ranked AS (
    SELECT
        c.id,
        c.new_src,
        c.new_tgt,
        CASE
            WHEN c.new_src = c.new_tgt AND c.old_src <> c.old_tgt THEN FALSE
            WHEN EXISTS (
                SELECT 1
                FROM graph.edge AS x
                WHERE
                    x.group_id = :project_id
                    AND x.source_id = c.new_src
                    AND x.target_id = c.new_tgt
                    AND x.rel_type = c.rel_type
                    AND (x.props = c.props OR x.edge_key = c.edge_key)
            ) THEN FALSE
            ELSE row_number() OVER (
                PARTITION BY
                    c.new_src,
                    c.new_tgt,
                    c.rel_type,
                    CASE
                        WHEN c.edge_key IS NOT NULL THEN 'k:' || c.edge_key
                        ELSE 'p:' || cast(c.props AS text)
                    END
                ORDER BY c.id
            ) = 1
        END AS keep
    FROM cand AS c
),

moved AS (
    UPDATE graph.edge
    SET source_id = k.new_src, target_id = k.new_tgt
    FROM
        (SELECT DISTINCT ON (r2.id)
            r2.id,
            r2.new_src,
            r2.new_tgt
        FROM ranked AS r2
        WHERE r2.keep
        ORDER BY r2.id) AS k
    WHERE edge.id = k.id
    RETURNING edge.id
),

dropped AS (
    DELETE FROM graph.edge
    WHERE
        edge.id IN (
            SELECT r3.id FROM ranked AS r3
            WHERE NOT r3.keep
        )
    RETURNING edge.id
),

stamped AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            jsonb_build_object(
                'spec_id_aliases',
                s.aliases,
                'last_build_id',
                cast(:build_id AS text)
            )
        )
    FROM survivors AS s
    WHERE node.id = s.new_id
    RETURNING node.id
),

gone AS (
    DELETE FROM graph.node
    WHERE node.id IN (SELECT p.old_id FROM pairs AS p)
    RETURNING node.id
)

SELECT count(*) AS written FROM survivors
