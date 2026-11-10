-- The spec_id aliases of the project, port of read_spec_aliases.cypher: each
-- spec node of :labels that has any, with its aliases, in spec_id order.
WITH found AS (
    SELECT
        n.spec_id,
        n.props -> 'spec_id_aliases' AS aliases
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:labels AS text[])
        AND jsonb_typeof(n.props -> 'spec_id_aliases') = 'array'
        AND jsonb_array_length(n.props -> 'spec_id_aliases') > 0
)

SELECT
    spec_id,
    aliases
FROM found
ORDER BY spec_id COLLATE "C"
