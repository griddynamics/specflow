-- Infer (realization): each subject node with its members and the container
-- nodes reached from them by one container path, each with the members it is
-- reached from; subjects without any container are left out. A rule with
-- several container paths runs this once per path (graph.build.infer).
-- Parameters: member_type (bind value), container_types (text[]: the path's
-- relations), container_either (boolean[]: per edge of the walk, whether it
-- may be walked either way), subject_labels, target_labels, bases (text[]).
-- The slot MEMBER_SYMMETRIC is a boolean literal (TRUE: the member relation is
-- walked either way; default FALSE). SEQUENCE is a boolean literal: TRUE, the
-- path is one edge of each relation of container_types in order (through spec
-- nodes); FALSE (the default), MIN_HOPS to MAX_HOPS edges of any of them.
-- MIN_HOPS and MAX_HOPS are the hop bounds (default 1 and 3; a sequence has
-- both equal to its length). Every edge of the path is stated or implied and
-- used once. Rows: uuid, spec_id, members (sorted uuids) and containers (a
-- JSON list of uuid and via, by uuid); ordered by spec_id, uuid.
WITH RECURSIVE subj AS (
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

mem AS (
    SELECT DISTINCT
        s.id AS sid,
        m.id AS mid,
        m.uuid AS muuid
    FROM subj AS s
    CROSS JOIN
        LATERAL (
            SELECT e.source_id AS mid
            FROM graph.edge AS e
            WHERE
                e.target_id = s.id
                AND e.rel_type = :member_type
                AND e.group_id = :project_id
                AND coalesce(e.props ->> 'basis', 'stated')
                = any(cast(:bases AS text[]))
            UNION ALL
            SELECT e.target_id AS mid
            FROM graph.edge AS e
            WHERE
            /*__MEMBER_SYMMETRIC__*/FALSE/*__MEMBER_SYMMETRIC__*/
                AND e.source_id = s.id
                AND e.source_id <> e.target_id
                AND e.rel_type = :member_type
                AND e.group_id = :project_id
                AND coalesce(e.props ->> 'basis', 'stated')
                = any(cast(:bases AS text[]))
        ) AS h
    INNER JOIN graph.node AS m
        ON
            h.mid = m.id
            AND m.group_id = :project_id
            AND m.kind = 'spec'
            AND m.uuid IS NOT NULL
),

walk (sid, mid, node_id, depth, edge_ids) AS (
    SELECT
        mem.sid,
        mem.mid,
        mem.mid AS node_id,
        0,
        cast(ARRAY[] AS bigint[])
    FROM mem
    UNION ALL
    SELECT
        w.sid,
        w.mid,
        hop.next_id,
        w.depth + 1,
        w.edge_ids || hop.edge_id
    FROM walk AS w
    CROSS JOIN
        LATERAL (
            SELECT
                e.id AS edge_id,
                e.target_id AS next_id
            FROM graph.edge AS e
            WHERE
                e.source_id = w.node_id
                AND e.rel_type = any(cast(:container_types AS text[]))
                AND (
                    NOT /*__SEQUENCE__*/FALSE/*__SEQUENCE__*/
                    OR e.rel_type
                    = (cast(:container_types AS text[]))[w.depth + 1]
                )
                AND e.group_id = :project_id
                AND coalesce(e.props ->> 'basis', 'stated')
                = any(cast(:bases AS text[]))
            UNION ALL
            SELECT
                e.id AS edge_id,
                e.source_id AS next_id
            FROM graph.edge AS e
            WHERE
                (cast(:container_either AS boolean[]))[w.depth + 1]
                AND e.target_id = w.node_id
                AND e.source_id <> e.target_id
                AND e.rel_type = any(cast(:container_types AS text[]))
                AND (
                    NOT /*__SEQUENCE__*/FALSE/*__SEQUENCE__*/
                    OR e.rel_type
                    = (cast(:container_types AS text[]))[w.depth + 1]
                )
                AND e.group_id = :project_id
                AND coalesce(e.props ->> 'basis', 'stated')
                = any(cast(:bases AS text[]))
        ) AS hop
    WHERE
        w.depth < /*__MAX_HOPS__*/3/*__MAX_HOPS__*/
        AND NOT (hop.edge_id = any(w.edge_ids))
        AND (
            NOT /*__SEQUENCE__*/FALSE/*__SEQUENCE__*/
            OR w.depth + 1 = /*__MAX_HOPS__*/3/*__MAX_HOPS__*/
            OR EXISTS (
                SELECT 1
                FROM graph.node AS x
                WHERE x.id = hop.next_id AND x.kind = 'spec'
            )
        )
),

reached AS (
    SELECT
        w.sid,
        c.uuid AS container_uuid,
        array_agg(DISTINCT m.uuid COLLATE "C" ORDER BY m.uuid COLLATE "C") AS via
    FROM walk AS w
    INNER JOIN graph.node AS c ON w.node_id = c.id
    INNER JOIN graph.node AS m ON w.mid = m.id
    WHERE
        w.depth >= /*__MIN_HOPS__*/1/*__MIN_HOPS__*/
        AND c.id <> w.sid
        AND c.group_id = :project_id
        AND c.kind = 'spec'
        AND c.uuid IS NOT NULL
        AND c.labels && cast(:target_labels AS text[])
    GROUP BY w.sid, c.uuid
),

conts AS (
    SELECT
        x.sid,
        jsonb_agg(
            jsonb_build_object('uuid', x.container_uuid, 'via', to_jsonb(x.via))
            ORDER BY x.container_uuid COLLATE "C"
        )
            AS containers
    FROM reached AS x
    GROUP BY x.sid
),

mems AS (
    SELECT
        x.sid,
        array_agg(DISTINCT x.muuid COLLATE "C" ORDER BY x.muuid COLLATE "C") AS members
    FROM mem AS x
    GROUP BY x.sid
),

found AS (
    SELECT
        s.uuid,
        s.spec_id,
        mems.members,
        conts.containers
    FROM subj AS s
    INNER JOIN mems ON s.id = mems.sid
    INNER JOIN conts ON s.id = conts.sid
)

SELECT
    uuid,
    spec_id,
    members,
    containers
FROM found
ORDER BY spec_id COLLATE "C", uuid COLLATE "C"
