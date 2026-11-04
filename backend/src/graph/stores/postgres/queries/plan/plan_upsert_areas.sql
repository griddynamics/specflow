-- Port of plan/queries/plan_upsert_areas.cypher. Persist: create or update
-- work areas by area id; a null property in row.properties removes it. Rows:
-- a JSON list of id, uuid and properties; ids are unique within a batch.
WITH r AS (
    SELECT
        e.item ->> 'id' AS id,
        e.item ->> 'uuid' AS uuid,
        coalesce(e.item -> 'properties', cast('{}' AS jsonb))
        || jsonb_build_object(
            'updated_at', cast(:now AS text),
            'last_run_id', cast(:run_id AS text)
        ) AS patch
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

updated AS (
    UPDATE graph.node
    SET props = graph.props_merge(node.props, r.patch)
    FROM r
    WHERE
        node.group_id = :project_id
        AND node.kind = 'foreign'
        AND node.labels[1] = 'WorkArea'
        AND node.props ->> 'area_id' = r.id
    RETURNING node.id
),

inserted AS (
    INSERT INTO graph.node (group_id, kind, labels, props)
    SELECT
        :project_id AS group_id,
        'foreign' AS kind,
        ARRAY['WorkArea'] AS labels,
        graph.props_merge(
            jsonb_build_object(
                'uuid', r.uuid,
                'area_id', r.id,
                'created_at', cast(:now AS text)
            ),
            r.patch
        ) AS props
    FROM r
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM graph.node AS x
            WHERE
                x.group_id = :project_id
                AND x.kind = 'foreign'
                AND x.labels[1] = 'WorkArea'
                AND x.props ->> 'area_id' = r.id
        )
    RETURNING node.id
)

SELECT count(*) AS written FROM r
