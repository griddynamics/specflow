// Delete one genre and its genre links (a registered node removal).
MATCH (g:Genre {name: $name})
DETACH DELETE g
