ALTER TABLE project_members
ADD CONSTRAINT ck_project_members_role
CHECK (role IN ('owner', 'collaborator'))
