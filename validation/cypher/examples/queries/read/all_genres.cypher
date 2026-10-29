// Every genre, for a picker: scan-shaped on purpose (policy.scan_shaped_queries).
MATCH (g:Genre)
RETURN g.name AS name
ORDER BY name
