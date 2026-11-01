-- Infer (endpoints): each subject node with the uuids of its target nodes;
-- subjects without any target are left out. The slot BRANCHES holds the UNION
-- branches the dialect renders from infer_endpoint_via and
-- infer_endpoint_mentions (each reads the CTE subj); its default is one
-- directed via branch. Parameters: subject_labels, target_labels, bases,
-- statuses (text[]) and the relation parameters of the branches.
WITH subj AS (
    SELECT
        n.id,
        n.uuid,
        n.spec_id
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid IS NOT NULL
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:subject_labels AS text[])
),

reached AS (
    /*__BRANCHES__*/SELECT
        s.id AS sid,
        m.uuid AS target
    FROM subj AS s
    INNER JOIN graph.edge AS r ON s.id = r.source_id
    INNER JOIN graph.node AS m ON r.target_id = m.id
    WHERE r.rel_type = :via_0/*__BRANCHES__*/
),

found AS (
    SELECT
        s.uuid,
        s.spec_id,
        array_agg(DISTINCT x.target ORDER BY x.target) AS targets
    FROM subj AS s
    INNER JOIN reached AS x ON s.id = x.sid
    GROUP BY s.uuid, s.spec_id
)

SELECT
    uuid,
    spec_id,
    targets
FROM found
ORDER BY spec_id COLLATE "C", uuid COLLATE "C"
