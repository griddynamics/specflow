-- fixture: clean under sql_rules -- declared table/columns, WHERE present,
-- an allow-listed delete, and no DROP outside a migration.
SELECT o.customer_id, o.status
FROM customer_order AS o
WHERE o.status = 'OPEN';

UPDATE customer_order SET status = 'CLOSED' WHERE customer_id = 1;

DELETE FROM cache_entry WHERE expires_at < 0;
