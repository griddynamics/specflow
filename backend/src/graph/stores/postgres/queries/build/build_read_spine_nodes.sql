-- Spine reachability: the spec nodes with their build-owned labels.
WITH found AS (
    SELECT
        n.uuid,
        n.spec_id,
        ARRAY(
            SELECT l FROM UNNEST(n.labels) AS l
            WHERE l = ANY(CAST(:labels AS text[]))
        ) AS labels
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid IS NOT NULL
)

SELECT
    uuid,
    spec_id,
    labels
FROM found
ORDER BY uuid COLLATE "C"
