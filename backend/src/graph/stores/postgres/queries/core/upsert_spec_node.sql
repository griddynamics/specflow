-- Port of core/queries/upsert_spec_node.cypher. Params: project_id, key, label, props (jsonb text).
-- One row always: label_mismatch, spec_id_changed; on either, nothing is written.
-- Label is set on create only. `embedding` in props goes to the vector column (JSON array of numbers; null removes it).
-- Both lookups seek node_spec_uuid_uq. A spec_id owned by another uuid raises 23505 (node_spec_specid_uq).
WITH p AS (
  SELECT CAST(:props AS jsonb) AS patch
), old AS (
  SELECT n.id, n.labels, n.spec_id
  FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid = :key
), chk AS (
  SELECT
    EXISTS (SELECT 1 FROM old WHERE NOT (CAST(:label AS text) = ANY (old.labels))) AS label_mismatch,
    EXISTS (SELECT 1 FROM old, p
            WHERE old.spec_id IS NOT NULL AND jsonb_exists(p.patch, 'spec_id')
              AND coalesce((p.patch ->> 'spec_id') <> old.spec_id, true)) AS spec_id_changed
), written AS (
  INSERT INTO graph.node AS n (group_id, kind, labels, props, embedding)
  SELECT :project_id, 'spec', ARRAY[CAST(:label AS text)],
         graph.props_merge(p.patch - 'embedding' - 'uuid', jsonb_build_object('uuid', CAST(:key AS text))),
         CASE WHEN jsonb_exists(p.patch, 'embedding') THEN CAST(p.patch ->> 'embedding' AS vector) END
  FROM p, chk
  WHERE NOT chk.label_mismatch AND NOT chk.spec_id_changed
  ON CONFLICT (group_id, uuid) WHERE kind = 'spec'
  DO UPDATE SET
    props = graph.props_merge(n.props, (SELECT patch - 'embedding' - 'uuid' FROM p)),
    embedding = CASE WHEN jsonb_exists((SELECT patch FROM p), 'embedding')
                     THEN CAST((SELECT patch ->> 'embedding' FROM p) AS vector) ELSE n.embedding END
  RETURNING 1
)
SELECT label_mismatch, spec_id_changed FROM chk;
