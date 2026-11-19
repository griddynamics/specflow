SELECT rolsuper OR rolbypassrls AS bypasses_rls
FROM pg_roles
WHERE rolname = :role_name
