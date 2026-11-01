-- ArcadeDB SQL (HTTP admin API), not PostgreSQL.
CREATE INDEX IF NOT EXISTS ON __TYPE__ (__COLUMNS__) LSM_VECTOR METADATA {"dimensions": __DIMENSION__, "similarity": "COSINE"}
