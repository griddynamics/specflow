CREATE FUNCTION project_members_legacy_owner()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role = 'owner' THEN
        NEW.role = 'admin';
    END IF;
    RETURN NEW;
END
$$
