-- Spine reachability: the spec nodes with their build-owned labels and the
-- stored values of the attributes the ontology's conditions name
-- (:attributes), as [name, value] pairs (absent values left out).
WITH found AS (
    SELECT
        n.uuid,
        n.spec_id,
        ARRAY(
            SELECT l FROM UNNEST(n.labels) AS l
            WHERE l = ANY(CAST(:labels AS text[]))
        ) AS labels,
        (
            SELECT
                coalesce(
                    jsonb_agg(
                        jsonb_build_array(a.name, n.props -> a.name)
                        ORDER BY a.position
                    ),
                    cast('[]' AS jsonb)
                )
            FROM
                unnest(cast(:attributes AS text[]))
                WITH ORDINALITY AS a (name, position)
            WHERE n.props -> a.name IS NOT NULL
        ) AS attributes
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid IS NOT NULL
)

SELECT
    uuid,
    spec_id,
    labels,
    attributes
FROM found
ORDER BY uuid COLLATE "C"
