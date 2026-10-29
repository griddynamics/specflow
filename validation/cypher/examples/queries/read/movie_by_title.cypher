// One movie by its indexed title.
MATCH (m:Movie {title: $title})
RETURN m.id AS id, m.title AS title, m.released AS released
