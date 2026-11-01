-- Infer: the swap's writes, one inferred edge per row. Parameters: rel_type,
-- inferred, rows: a JSON list of source, target, edge_key, rule,
-- either_direction, uuid, created_at, props and ref. A row is skipped when an
-- edge with its key exists that is not an inferred edge of the same rule, or,
-- with row.either_direction, when the two nodes are already linked by a non-
-- inferred edge of the relation. The edge is created with uuid and created_at;
-- props merge on create and refresh. Rows of one edge are folded in order (the
-- first row's uuid and created_at count). Row: written and refs (the ref of
-- each row written).
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'source' AS source,
        e.item ->> 'target' AS target,
        e.item ->> 'edge_key' AS edge_key,
        e.item ->> 'rule' AS rule_name,
        coalesce(cast(e.item ->> 'either_direction' AS boolean), FALSE)
            AS either_direction,
        e.item ->> 'uuid' AS uuid,
        e.item -> 'created_at' AS created_at,
        coalesce(e.item -> 'props', cast('{}' AS jsonb)) AS props,
        e.item -> 'ref' AS row_ref
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

ends AS (
    SELECT
        r.edge_key,
        r.rule_name,
        r.either_direction,
        r.uuid,
        r.created_at,
        r.props,
        r.row_ref,
        r.ord,
        s.id AS source_id,
        t.id AS target_id
    FROM r
    INNER JOIN graph.node AS s
        ON s.group_id = :project_id AND s.kind = 'spec' AND r.source = s.uuid
    INNER JOIN graph.node AS t
        ON t.group_id = :project_id AND t.kind = 'spec' AND r.target = t.uuid
),

allowed AS (
    SELECT x.*
    FROM ends AS x
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.edge AS k
            WHERE
                k.group_id = :project_id
                AND k.rel_type = :rel_type
                AND k.edge_key = x.edge_key
                AND (
                    coalesce(k.props ->> 'basis', '') <> :inferred
                    OR coalesce(k.props ->> 'inference_rule', '') <> x.rule_name
                )
        )
        AND NOT (
            x.either_direction
            AND EXISTS (
                SELECT 1
                FROM graph.edge AS y
                WHERE
                    y.group_id = :project_id
                    AND y.rel_type = :rel_type
                    AND (
                        (
                            y.source_id = x.source_id
                            AND y.target_id = x.target_id
                        )
                        OR (
                            y.source_id = x.target_id
                            AND y.target_id = x.source_id
                        )
                    )
                    AND coalesce(y.props ->> 'basis', '') <> :inferred
            )
        )
),

folded AS (
    SELECT
        a.edge_key,
        a.source_id,
        a.target_id,
        (array_agg(a.uuid ORDER BY a.ord))[1] AS uuid,
        (array_agg(a.created_at ORDER BY a.ord))[1] AS created_at,
        graph.props_fold(array_agg(a.props ORDER BY a.ord)) AS patch
    FROM allowed AS a
    GROUP BY a.edge_key, a.source_id, a.target_id
),

updated AS (
    UPDATE graph.edge
    SET props = graph.props_merge(edge.props, f.patch)
    FROM folded AS f
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = :rel_type
        AND edge.edge_key = f.edge_key
        AND edge.source_id = f.source_id
        AND edge.target_id = f.target_id
    RETURNING edge.id
),

inserted AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        cast(:rel_type AS text) AS rel_type,
        f.source_id,
        f.target_id,
        graph.props_merge(
            jsonb_build_object(
                'edge_key',
                f.edge_key,
                'uuid',
                f.uuid,
                'created_at',
                f.created_at
            ),
            f.patch
        ) AS props
    FROM folded AS f
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.edge AS x
            WHERE
                x.group_id = :project_id
                AND x.rel_type = :rel_type
                AND x.edge_key = f.edge_key
                AND x.source_id = f.source_id
                AND x.target_id = f.target_id
        )
    ON CONFLICT (group_id, rel_type, edge_key, source_id, target_id) DO NOTHING
    RETURNING edge.id
)

SELECT
    count(*) AS written,
    coalesce(
        jsonb_agg(a.row_ref ORDER BY a.ord) FILTER (
            WHERE a.row_ref IS NOT NULL
        ),
        cast('[]' AS jsonb)
    ) AS refs
FROM allowed AS a
