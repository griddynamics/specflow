-- Verification: spec nodes with their build-owned properties and labels, and
-- the stored values of the attributes the ontology's conditions name
-- (:attributes), as [name, value] pairs (absent values left out). id is the
-- text of the node id; evidence lists the fragment ids of its REFERENCED_BY
-- relationships.
WITH found AS (
    SELECT
        cast(n.id AS text) AS id,
        n.uuid,
        n.spec_id,
        n.props -> 'name' AS name,
        n.props -> 'identity_key' AS identity_key,
        n.props -> 'identity_aliases' AS identity_aliases,
        n.props -> 'spec_id_aliases' AS spec_id_aliases,
        n.props -> 'label_disagreement' AS label_disagreement,
        array(
            SELECT l FROM unnest(n.labels) AS l
            WHERE l = any(cast(:labels AS text[]))
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
        ) AS attributes,
        array(
            SELECT f.fragment_id
            FROM graph.edge AS e
            INNER JOIN graph.node AS f ON e.target_id = f.id
            WHERE
                e.source_id = n.id
                AND e.rel_type = 'REFERENCED_BY'
                AND f.kind = 'fragment'
                AND f.group_id = :project_id
        ) AS evidence
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec'
)

SELECT
    id,
    uuid,
    spec_id,
    name,
    identity_key,
    identity_aliases,
    spec_id_aliases,
    label_disagreement,
    labels,
    attributes,
    evidence
FROM found
ORDER BY id COLLATE "C"
