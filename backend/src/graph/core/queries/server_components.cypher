// Server name, versions and edition, for the version gate (unscoped).
CALL dbms.components()
YIELD name, versions, edition
RETURN name, versions, edition
