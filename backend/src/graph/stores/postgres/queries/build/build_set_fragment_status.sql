-- Persist: set the status and error code of fragments in a batch (a null error
-- code removes it). Parameters: project_id, build_id, rows: a JSON list of
-- fragment_id, status and error_code. written counts the rows that found their
-- fragment.
WITH r AS (
    SELECT
        e.item ->> 'fragment_id' AS fragment_id,
        e.item ->> 'status' AS status,
        e.item ->> 'error_code' AS error_code
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            jsonb_build_object(
                'status',
                r.status,
                'error_code',
                r.error_code,
                'last_build_id',
                cast(:build_id AS text)
            )
        )
    FROM (
        SELECT DISTINCT ON (r1.fragment_id)
            r1.fragment_id,
            r1.status,
            r1.error_code
        FROM r AS r1
    ) AS r
    WHERE
        node.group_id = :project_id
        AND node.kind = 'fragment'
        AND node.fragment_id = r.fragment_id
    RETURNING node.id
)

SELECT count(*) AS written
FROM r
INNER JOIN graph.node AS f
    ON
        f.group_id = :project_id
        AND f.kind = 'fragment'
        AND r.fragment_id = f.fragment_id
