-- Infer: the swap's deletes, the inferred edge row.uuid of rule row.rule under
-- row.edge_key. Parameters: rel_type, inferred, rows: a JSON list of edge_key,
-- rule, uuid and ref. Row: written and refs (the ref of each row that deleted
-- an edge).
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'edge_key' AS edge_key,
        e.item ->> 'rule' AS rule_name,
        e.item ->> 'uuid' AS uuid,
        e.item -> 'ref' AS row_ref
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

hits AS (
    SELECT
        r.row_ref,
        r.ord,
        x.id
    FROM r
    INNER JOIN graph.edge AS x
        ON
            x.group_id = :project_id
            AND x.rel_type = :rel_type
            AND r.edge_key = x.edge_key
            AND x.props ->> 'basis' = :inferred
            AND x.props ->> 'inference_rule' = r.rule_name
            AND (x.props ->> 'uuid' = r.uuid OR x.props ->> 'uuid' IS NULL)
),

deleted AS (
    DELETE FROM graph.edge
    WHERE edge.id IN (SELECT h.id FROM hits AS h)
    RETURNING edge.id
)

SELECT
    count(*) AS written,
    coalesce(
        jsonb_agg(h.row_ref ORDER BY h.ord) FILTER (
            WHERE h.row_ref IS NOT NULL
        ),
        cast('[]' AS jsonb)
    ) AS refs
FROM hits AS h
