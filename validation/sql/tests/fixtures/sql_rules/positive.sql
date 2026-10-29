-- fixture: violates every sql_rules check.
DELETE FROM customer_order;
UPDATE audit_log SET request_id = 'x' WHERE ts = 1;
DELETE FROM app_account WHERE id = 1;
DROP TABLE customer_order;
SELECT o.made_up_column FROM customer_order AS o;
SELECT x.id FROM nope_table AS x;
SELECT z.mystery FROM customer_order AS o;
-- noqa: disable this on purpose
