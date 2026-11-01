-- Infer: the text of fragments by id. Parameter fragment_ids: text[].
WITH found AS (
    SELECT
        f.fragment_id,
        coalesce(f.props ->> 'text', '') AS text
    FROM graph.node AS f
    INNER JOIN
        unnest(cast(:fragment_ids AS text[])) AS w (fragment_id)
        ON f.fragment_id = w.fragment_id
    WHERE f.group_id = :project_id AND f.kind = 'fragment'
)

SELECT
    fragment_id,
    text
FROM found
ORDER BY fragment_id COLLATE "C"
