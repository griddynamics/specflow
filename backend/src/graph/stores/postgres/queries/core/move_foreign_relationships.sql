-- Port of core/queries/move_foreign_relationships.cypher. Parameters:
-- project_id, from_key, from_label, to_key, to_label, owned_types (text[]).
-- Moves every relationship of the old node whose type is not owned onto the
-- new node, keeping type, direction and properties (the row is re-pointed, so
-- edge_key and id survive). An equal edge (or one with the same edge_key)
-- already on the new node, an equal edge earlier in the moved set and an edge
-- between old and new are only deleted; a self-loop of old becomes a
-- self-loop of new (canonical).
-- One row always: moved counts every relationship taken off old.
WITH old AS (
    SELECT n.id
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = :from_key
        AND cast(:from_label AS text) = any(n.labels)
),

new AS (
    SELECT n.id
    FROM graph.node AS n
    CROSS JOIN old
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = :to_key
        AND cast(:to_label AS text) = any(n.labels)
        AND n.id <> old.id
),

cand AS (
    SELECT
        e.id,
        e.rel_type,
        e.props,
        e.edge_key,
        CASE WHEN e.source_id = old.id THEN new.id ELSE e.source_id END
            AS new_src,
        CASE WHEN e.target_id = old.id THEN new.id ELSE e.target_id END
            AS new_tgt,
        (e.source_id = new.id OR e.target_id = new.id) AS between_ends
    FROM graph.edge AS e
    CROSS JOIN old
    CROSS JOIN new
    WHERE
        e.group_id = :project_id
        AND (e.source_id = old.id OR e.target_id = old.id)
        AND NOT (e.rel_type = any(cast(:owned_types AS text[])))
),

ranked AS (
    SELECT
        c.id,
        c.new_src,
        c.new_tgt,
        CASE
            WHEN c.between_ends THEN FALSE
            WHEN EXISTS (
                SELECT 1
                FROM graph.edge AS x
                WHERE
                    x.group_id = :project_id
                    AND x.source_id = c.new_src
                    AND x.target_id = c.new_tgt
                    AND x.rel_type = c.rel_type
                    AND (
                        x.props = c.props
                        OR x.edge_key = c.edge_key
                    )
            ) THEN FALSE
            ELSE row_number() OVER (
                PARTITION BY
                    c.between_ends, c.new_src, c.new_tgt, c.rel_type, c.props
                ORDER BY c.id
            ) = 1
        END AS keep
    FROM cand AS c
),

moved AS (
    UPDATE graph.edge AS e
    SET source_id = r.new_src, target_id = r.new_tgt
    FROM ranked AS r
    WHERE e.id = r.id AND r.keep
    RETURNING e.id
),

dropped AS (
    DELETE FROM graph.edge AS e
    USING ranked AS r
    WHERE e.id = r.id AND NOT r.keep
    RETURNING e.id
)

SELECT count(*) AS moved FROM ranked
