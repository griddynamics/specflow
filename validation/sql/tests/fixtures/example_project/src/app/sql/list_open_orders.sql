SELECT
    o.id,
    o.customer_id
FROM customer_order AS o
WHERE o.status = 'OPEN';
