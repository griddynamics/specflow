-- Syntax pieces of the PostgreSQL dialect (graph.stores.postgres.dialect) as
-- slots, so its renderers write no query text: TRUE and FALSE are the boolean
-- literals of a slot that says whether a relation is walked either way, UNION
-- separates the branches of a union.
SELECT /*__TRUE__*/TRUE/*__TRUE__*/ AS either_way
/*__UNION__*/ UNION /*__UNION__*/
SELECT /*__FALSE__*/FALSE/*__FALSE__*/ AS either_way
