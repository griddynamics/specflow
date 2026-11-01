-- Port of core/queries/upsert_spec_node.cypher. Parameters: project_id, key,
-- label, props (jsonb text). One row always: label_mismatch (a node with this
-- uuid has another domain label) and spec_id_changed (it has a spec_id and
-- :props would set another one or remove it); on either nothing is written.
-- The label is set on create only. `embedding` in :props goes to the vector
-- column (a JSON array of numbers; null removes it), the other properties
-- merge as SET += does (a null removes the property). Both lookups seek
-- node_spec_uuid_uq; a spec_id another uuid holds raises 23505
-- (node_spec_specid_uq).
WITH p AS (
    SELECT cast(:props AS jsonb) AS patch
),

old AS (
    SELECT
        n.id,
        n.labels,
        n.spec_id
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :key
),

chk AS (
    SELECT
        exists(
            SELECT 1 FROM old
            WHERE NOT (cast(:label AS text) = any(old.labels))
        ) AS label_mismatch,
        exists(
            SELECT 1
            FROM old
            CROSS JOIN p
            WHERE
                old.spec_id IS NOT NULL
                AND jsonb_exists(p.patch, 'spec_id')
                AND coalesce((p.patch ->> 'spec_id') <> old.spec_id, TRUE)
        ) AS spec_id_changed
),

written AS (
    INSERT INTO graph.node (group_id, kind, labels, props, embedding)
    SELECT
        :project_id AS group_id,
        'spec' AS kind,
        ARRAY[cast(:label AS text)] AS labels,
        graph.props_merge(
            p.patch - 'embedding' - 'uuid',
            jsonb_build_object('uuid', cast(:key AS text))
        ) AS props,
        CASE
            WHEN jsonb_exists(p.patch, 'embedding')
                THEN cast(p.patch ->> 'embedding' AS vector)
        END AS embedding
    FROM p
    CROSS JOIN chk
    WHERE NOT chk.label_mismatch AND NOT chk.spec_id_changed
    ON CONFLICT (group_id, uuid) WHERE kind = 'spec'
    DO UPDATE
        SET
            props = graph.props_merge(
                node.props, (SELECT p.patch - 'embedding' - 'uuid' FROM p)
            ),
            embedding = CASE
                WHEN jsonb_exists((SELECT p.patch FROM p), 'embedding')
                    THEN cast((SELECT p.patch ->> 'embedding' FROM p) AS vector)
                ELSE node.embedding
            END
    RETURNING node.id
)

SELECT
    label_mismatch,
    spec_id_changed
FROM chk
