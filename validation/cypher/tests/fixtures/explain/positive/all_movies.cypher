// Fixture (positive): a registered scan-shaped query; its label scan of `Movie` is exempt
// because this file's stem matches a policy.scan_shaped_queries entry.
MATCH (m:Movie)
RETURN m.title AS title
