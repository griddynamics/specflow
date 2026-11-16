-- Port of gap/queries/gap_read_gaps.cypher. Gaps whose status is in
-- :statuses (and whose gap id is in :gap_ids, when that list is not empty),
-- each with its ABOUT and IMPACTS nodes (relation, spec id, uuid, labels,
-- name, the documents holding the node's evidence, edge key, added_by_run),
-- its CITES witnesses (fragment, document, quote, lines, quote hash, edge
-- key), its follow-up chain (continues: the gap is the source) and the
-- properties of the clarifications that answer it. The library sorts and
-- filters further.
SELECT
    g.props AS gap,
    coalesce(
        (
            SELECT
                jsonb_agg(
                    jsonb_build_object(
                        'relation', r.rel_type,
                        'spec_id', n.spec_id,
                        'uuid', n.uuid,
                        'labels', to_jsonb(n.labels),
                        'name', n.props ->> 'name',
                        'documents', coalesce(
                            (
                                SELECT
                                    jsonb_agg(
                                        DISTINCT ef.props ->> 'document_id'
                                    )
                                FROM graph.edge AS ev
                                INNER JOIN graph.node AS ef
                                    ON
                                        ev.target_id = ef.id
                                        AND ef.kind = 'fragment'
                                        AND ef.group_id = :project_id
                                WHERE
                                    ev.group_id = :project_id
                                    AND ev.source_id = n.id
                                    AND ev.rel_type = 'REFERENCED_BY'
                            ),
                            cast('[]' AS jsonb)
                        ),
                        'edge_key', r.edge_key,
                        'added_by_run', r.props ->> 'added_by_run'
                    )
                    ORDER BY r.edge_key
                )
            FROM graph.edge AS r
            INNER JOIN graph.node AS n
                ON
                    r.target_id = n.id
                    AND n.kind = 'spec'
                    AND n.group_id = :project_id
            WHERE
                r.group_id = :project_id
                AND r.source_id = g.id
                AND r.rel_type IN ('ABOUT', 'IMPACTS')
        ),
        cast('[]' AS jsonb)
    ) AS nodes,
    coalesce(
        (
            SELECT
                jsonb_agg(
                    jsonb_build_object(
                        'fragment_id', f.fragment_id,
                        'document_id', f.props ->> 'document_id',
                        'quote', c.props ->> 'quote',
                        'line_start', c.props -> 'line_start',
                        'line_end', c.props -> 'line_end',
                        'quote_hash', c.props ->> 'quote_hash',
                        'edge_key', c.edge_key
                    )
                    ORDER BY c.edge_key
                )
            FROM graph.edge AS c
            INNER JOIN graph.node AS f
                ON
                    c.target_id = f.id
                    AND f.kind = 'fragment'
                    AND f.group_id = :project_id
            WHERE
                c.group_id = :project_id
                AND c.source_id = g.id
                AND c.rel_type = 'CITES'
        ),
        cast('[]' AS jsonb)
    ) AS cites,
    coalesce(
        (
            SELECT
                jsonb_agg(
                    jsonb_build_object(
                        'gap_id', o.props ->> 'gap_id',
                        'reason', u.props ->> 'reason',
                        'continues', u.source_id = g.id
                    )
                    ORDER BY o.props ->> 'gap_id'
                )
            FROM graph.edge AS u
            INNER JOIN graph.node AS o
                ON
                    o.id = CASE
                        WHEN u.source_id = g.id THEN u.target_id
                        ELSE u.source_id
                    END
                    AND o.kind = 'foreign'
                    AND o.labels[1] = 'Gap'
                    AND o.group_id = :project_id
            WHERE
                u.group_id = :project_id
                AND u.rel_type = 'FOLLOWS_UP'
                AND (u.source_id = g.id OR u.target_id = g.id)
        ),
        cast('[]' AS jsonb)
    ) AS follow_ups,
    coalesce(
        (
            SELECT jsonb_agg(k.props ORDER BY k.props ->> 'clr_id')
            FROM graph.edge AS a
            INNER JOIN graph.node AS k
                ON
                    a.source_id = k.id
                    AND k.kind = 'foreign'
                    AND k.labels[1] = 'Clarification'
                    AND k.group_id = :project_id
            WHERE
                a.group_id = :project_id
                AND a.rel_type = 'ANSWERS'
                AND a.target_id = g.id
        ),
        cast('[]' AS jsonb)
    ) AS clarifications
FROM graph.node AS g
WHERE
    g.group_id = :project_id
    AND g.kind = 'foreign'
    AND g.labels[1] = 'Gap'
    AND g.props ->> 'status' = any(cast(:statuses AS text[]))
    AND (
        cardinality(cast(:gap_ids AS text[])) = 0
        OR g.props ->> 'gap_id' = any(cast(:gap_ids AS text[]))
    )
ORDER BY g.props ->> 'gap_id'
