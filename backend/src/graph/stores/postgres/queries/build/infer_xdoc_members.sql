-- Infer (cross_document): each hub of :subject_labels with its name and
-- identity keys and its members: nodes of :member_labels linked to it by one
-- stated or implied edge of a :via relation in either direction and, with
-- :mentions, those grounded in a fragment the hub is MENTIONED_IN; with
-- :mentions also the other nodes grounded in a fragment the hub is grounded
-- in itself (REFERENCED_BY), each with shared (those fragment ids), its name,
-- its :quote attribute and its statements, so the caller keeps only those
-- whose own text names the hub; each member with the documents of its
-- evidence fragments whose status is in :statuses. Members without such a
-- fragment are left out, and so are hubs with fewer than two members.
-- Parameters: subject_labels, member_labels, via, bases, statuses (text[]),
-- mentions (boolean), quote (text). Rows: uuid, spec_id, name, keys and
-- members (a JSON list by uuid); ordered by spec_id, uuid.
WITH hubs AS (
    SELECT
        n.id,
        n.uuid,
        n.spec_id,
        n.props
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid IS NOT NULL
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:subject_labels AS text[])
),

eligible AS (
    SELECT
        m.id,
        m.uuid,
        m.props
    FROM graph.node AS m
    WHERE
        m.group_id = :project_id
        AND m.kind = 'spec'
        AND m.uuid IS NOT NULL
        AND m.spec_id IS NOT NULL
        AND m.labels && cast(:member_labels AS text[])
),

linked AS (
    SELECT
        h.id AS hub_id,
        r.target_id AS member_id
    FROM hubs AS h
    INNER JOIN graph.edge AS r ON h.id = r.source_id
    WHERE
        r.rel_type = any(cast(:via AS text[]))
        AND r.group_id = :project_id
        AND coalesce(r.props ->> 'basis', 'stated') = any(cast(:bases AS text[]))
    UNION
    SELECT
        h.id AS hub_id,
        r.source_id AS member_id
    FROM hubs AS h
    INNER JOIN graph.edge AS r ON h.id = r.target_id
    WHERE
        r.rel_type = any(cast(:via AS text[]))
        AND r.group_id = :project_id
        AND coalesce(r.props ->> 'basis', 'stated') = any(cast(:bases AS text[]))
),

mentioned AS (
    SELECT
        h.id AS hub_id,
        e.source_id AS member_id
    FROM hubs AS h
    INNER JOIN graph.edge AS l
        ON
            h.id = l.source_id
            AND l.rel_type = 'MENTIONED_IN'
            AND l.group_id = :project_id
    INNER JOIN graph.node AS f
        ON
            l.target_id = f.id
            AND f.kind = 'fragment'
            AND f.group_id = :project_id
            AND f.props ->> 'status' = any(cast(:statuses AS text[]))
    INNER JOIN graph.edge AS e
        ON
            f.id = e.target_id
            AND e.rel_type = 'REFERENCED_BY'
            AND e.group_id = :project_id
    WHERE cast(:mentions AS boolean) AND e.source_id <> h.id
),

named AS (
    SELECT
        x.hub_id,
        x.member_id
    FROM (
        SELECT
            hub_id,
            member_id
        FROM linked
        UNION
        SELECT
            hub_id,
            member_id
        FROM mentioned
    ) AS x
    INNER JOIN eligible AS m ON x.member_id = m.id
),

grounded AS (
    SELECT
        h.id AS hub_id,
        q.source_id AS member_id,
        array_agg(DISTINCT hf.fragment_id COLLATE "C" ORDER BY hf.fragment_id COLLATE "C") AS shared
    FROM hubs AS h
    INNER JOIN graph.edge AS o
        ON
            h.id = o.source_id
            AND o.rel_type = 'REFERENCED_BY'
            AND o.group_id = :project_id
    INNER JOIN graph.node AS hf
        ON
            o.target_id = hf.id
            AND hf.kind = 'fragment'
            AND hf.group_id = :project_id
            AND hf.props ->> 'status' = any(cast(:statuses AS text[]))
    INNER JOIN graph.edge AS q
        ON
            hf.id = q.target_id
            AND q.rel_type = 'REFERENCED_BY'
            AND q.group_id = :project_id
    INNER JOIN eligible AS s ON q.source_id = s.id
    WHERE
        cast(:mentions AS boolean)
        AND s.id <> h.id
        AND NOT EXISTS (
            SELECT 1
            FROM named AS x
            WHERE x.hub_id = h.id AND x.member_id = s.id
        )
    GROUP BY h.id, q.source_id
),

items AS (
    SELECT
        hub_id,
        member_id,
        cast(NULL AS text []) AS shared
    FROM named
    UNION ALL
    SELECT
        hub_id,
        member_id,
        shared
    FROM grounded
),

documented AS (
    SELECT
        i.hub_id,
        i.member_id,
        i.shared,
        array_agg(DISTINCT f.document_id COLLATE "C" ORDER BY f.document_id COLLATE "C") AS documents
    FROM items AS i
    INNER JOIN graph.edge AS e
        ON
            i.member_id = e.source_id
            AND e.rel_type = 'REFERENCED_BY'
            AND e.group_id = :project_id
    INNER JOIN graph.node AS f
        ON
            e.target_id = f.id
            AND f.kind = 'fragment'
            AND f.group_id = :project_id
            AND f.props ->> 'status' = any(cast(:statuses AS text[]))
            AND f.document_id IS NOT NULL
    GROUP BY i.hub_id, i.member_id, i.shared
),

members AS (
    SELECT
        d.hub_id,
        jsonb_agg(
            CASE
                WHEN d.shared IS NULL
                    THEN
                        jsonb_build_object(
                            'uuid', m.uuid, 'documents', to_jsonb(d.documents)
                        )
                ELSE
                    jsonb_build_object(
                        'uuid', m.uuid,
                        'documents', to_jsonb(d.documents),
                        'shared', to_jsonb(d.shared),
                        'name', m.props -> 'name',
                        'quote', m.props -> cast(:quote AS text),
                        'statements', m.props -> 'statements'
                    )
            END
            ORDER BY m.uuid COLLATE "C"
        ) AS members
    FROM documented AS d
    INNER JOIN eligible AS m ON d.member_id = m.id
    GROUP BY d.hub_id
    HAVING count(*) > 1
),

found AS (
    SELECT
        h.uuid,
        h.spec_id,
        h.props -> 'name' AS name,
        (
            SELECT coalesce(jsonb_agg(k.value ORDER BY k.position), cast('[]' AS jsonb))
            FROM
                jsonb_array_elements(
                    jsonb_build_array(h.props -> 'identity_key')
                    || coalesce(h.props -> 'identity_aliases', cast('[]' AS jsonb))
                ) WITH ORDINALITY AS k (value, position)
            WHERE jsonb_typeof(k.value) <> 'null'
        ) AS keys,
        mb.members
    FROM hubs AS h
    INNER JOIN members AS mb ON h.id = mb.hub_id
)

SELECT
    uuid,
    spec_id,
    name,
    keys,
    members
FROM found
ORDER BY spec_id COLLATE "C", uuid COLLATE "C"
