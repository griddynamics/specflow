-- Inheritance (data-model.md "Attributes"): for each request of :requests (a
-- JSON list of uuid, relation and attributes), the spec nodes one hop away
-- from the node (by uuid) over the request's inherit relation, with their
-- labels and the stored values of the request's attributes as [name, value]
-- pairs (a value not stored is null; the reader reads the pairs as a map, so
-- their order is free). The reader keeps the owner-pair targets.
WITH requests AS (
    SELECT
        item.value ->> 'uuid' AS node_uuid,
        item.value ->> 'relation' AS relation,
        coalesce(item.value -> 'attributes', cast('[]' AS jsonb)) AS attributes
    FROM jsonb_array_elements(cast(:requests AS jsonb)) AS item
),

found AS (
    SELECT
        req.node_uuid AS uuid,
        req.relation,
        o.spec_id,
        array_prepend('SpecNode', o.labels) AS labels,
        (
            SELECT
                coalesce(
                    jsonb_agg(jsonb_build_array(a.name, o.props -> a.name)),
                    cast('[]' AS jsonb)
                )
            FROM jsonb_array_elements_text(req.attributes) AS a (name)
        ) AS attribute_values
    FROM requests AS req
    INNER JOIN graph.node AS n
        ON
            n.group_id = :project_id
            AND n.kind = 'spec'
            AND req.node_uuid = n.uuid
    INNER JOIN graph.edge AS r
        ON
            n.id = r.source_id
            AND req.relation = r.rel_type
            AND r.group_id = :project_id
    INNER JOIN graph.node AS o
        ON
            r.target_id = o.id
            AND o.group_id = :project_id
            AND o.kind = 'spec'
            AND o.spec_id IS NOT NULL
)

SELECT
    uuid,
    relation,
    spec_id,
    labels,
    attribute_values
FROM found
ORDER BY uuid COLLATE "C", relation COLLATE "C", spec_id COLLATE "C"
