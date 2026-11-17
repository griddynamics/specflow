CREATE UNIQUE INDEX IF NOT EXISTS ux_project_members_one_owner
ON project_members (project_id)
WHERE role = 'owner'
