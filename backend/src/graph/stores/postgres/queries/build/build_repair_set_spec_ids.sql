-- Known limit: the unique index on (group_id, spec_id) is checked per row, so a
-- batch that swaps the spec_ids of two nodes raises a unique violation (not
-- seen in practice; docs/configuration.md, PostgreSQL).
-- Repair: set the spec_id and aliases of nodes addressed by id (text of the
-- node id). Parameter rows: a JSON list of id, spec_id and aliases. written
-- counts the rows that found their node.
WITH r AS (
    SELECT
        e.ord,
        e.item ->> 'id' AS id,
        e.item -> 'spec_id' AS spec_id,
        e.item -> 'aliases' AS aliases
    FROM
        jsonb_array_elements(cast(:rows AS jsonb)) WITH ORDINALITY
            AS e (item, ord)
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            jsonb_build_object(
                'spec_id', k.spec_id, 'spec_id_aliases', k.aliases
            )
        )
    FROM
        (SELECT DISTINCT ON (r1.id)
            r1.id,
            r1.spec_id,
            r1.aliases
        FROM r AS r1
        ORDER BY r1.id ASC, r1.ord DESC) AS k
    WHERE
        cast(node.id AS text) = k.id
        AND node.group_id = :project_id
        AND node.kind = 'spec'
    RETURNING node.id
)

SELECT count(*) AS written
FROM r
INNER JOIN graph.node AS x
    ON
        cast(x.id AS text) = r.id
        AND x.group_id = :project_id
        AND x.kind = 'spec'
