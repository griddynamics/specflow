// Fixture (positive): a unique-constrained anchor -- an index seek, never a label scan.
MATCH (g:Genre {name: $name})
RETURN g.name AS name
