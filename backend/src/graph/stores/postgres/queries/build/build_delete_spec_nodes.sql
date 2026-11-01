-- Persist: delete spec nodes in a batch (their relationships cascade).
-- Parameter rows: a JSON list of uuid. written counts the deleted nodes.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.group_id = :project_id
        AND node.kind = 'spec'
        AND node.uuid IN (
            SELECT e.item ->> 'uuid'
            FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
        )
    RETURNING node.id
)

SELECT count(*) AS written FROM deleted
