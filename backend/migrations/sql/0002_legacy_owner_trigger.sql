CREATE TRIGGER project_members_legacy_owner
BEFORE INSERT ON project_members
FOR EACH ROW EXECUTE FUNCTION project_members_legacy_owner()
