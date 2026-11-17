CREATE POLICY agent_project_scope ON project_invites
TO agent_role
USING (
    project_id = nullif(current_setting('app.project_id', true), '')::uuid
)
WITH CHECK (
    project_id = nullif(current_setting('app.project_id', true), '')::uuid
)
