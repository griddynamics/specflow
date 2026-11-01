-- Port of core/queries/upsert_relationship.cypher. Parameters: project_id,
-- s_key, s_label, t_key, t_label, rel_type, key, props. SOURCE and TARGET are
-- slots for the node matches the dialect renders from the match_*_node
-- fragments; their default is the match of a spec node.
-- No row when an end is missing. conflict is true when an edge of this type
-- and edge_key joins other endpoints; nothing is written then. The same
-- endpoints and key merge the properties (SET r += props).
WITH s AS (
    /*__SOURCE__*/SELECT n.id FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = :s_key
        AND cast(:s_label AS text) = any(n.labels)/*__SOURCE__*/
),

t AS (
    /*__TARGET__*/SELECT n.id FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = :t_key
        AND cast(:t_label AS text) = any(n.labels)/*__TARGET__*/
),

ends AS (
    SELECT
        s.id AS sid,
        t.id AS tid
    FROM s
    CROSS JOIN t
),

other AS (
    SELECT count(*) AS conflicts
    FROM graph.edge AS e
    CROSS JOIN ends
    WHERE
        e.group_id = :project_id
        AND e.rel_type = :rel_type
        AND e.edge_key = :key
        AND NOT (e.source_id = ends.sid AND e.target_id = ends.tid)
),

written AS (
    INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props)
    SELECT
        :project_id AS group_id,
        cast(:rel_type AS text) AS rel_type,
        ends.sid AS source_id,
        ends.tid AS target_id,
        graph.props_merge(
            cast(:props AS jsonb) - 'edge_key',
            jsonb_build_object('edge_key', cast(:key AS text))
        ) AS props
    FROM ends
    CROSS JOIN other
    WHERE other.conflicts = 0
    ON CONFLICT (group_id, rel_type, edge_key)
    DO UPDATE
        SET
            props
            = graph.props_merge(edge.props, cast(:props AS jsonb) - 'edge_key')
        WHERE
        edge.source_id = (SELECT ends.sid FROM ends)
        AND edge.target_id = (SELECT ends.tid FROM ends)
    RETURNING edge.id
)

SELECT other.conflicts > 0 AS conflict
FROM other
CROSS JOIN ends
