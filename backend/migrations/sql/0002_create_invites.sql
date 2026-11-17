CREATE TABLE IF NOT EXISTS project_invites (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    email_norm text NOT NULL,
    invited_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_project_invites_email_norm
    CHECK (email_norm = LOWER(BTRIM(email_norm))),
    CONSTRAINT fk_project_invites_project_id_projects
    FOREIGN KEY (project_id) REFERENCES projects (id),
    CONSTRAINT fk_project_invites_invited_by_users
    FOREIGN KEY (invited_by) REFERENCES users (id),
    CONSTRAINT pk_project_invites PRIMARY KEY (id),
    CONSTRAINT uq_project_invites_project_id_email_norm
    UNIQUE (project_id, email_norm)
)
