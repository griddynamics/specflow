-- The schema of the MERGED 0001_initial (commit 5a23613, the revision that also created
-- project_invites, ux_project_members_one_owner and ix_users_email_lower). Some databases were
-- built from it and are stamped 0001_initial; revision 0002_project_collaborators must change
-- nothing there. Test fixture only: rendered offline from
--   git show 5a23613:backend/migrations/versions/0001_initial.py
-- with "alembic upgrade head --sql" (agent role "specflow_agent", alembic_version bookkeeping removed).
-- The live test replaces "specflow_agent" by its test role, runs the statements, stamps
-- 0001_initial and upgrades to head. Never edit; never run against a real database.

-- agent role existence check skipped (offline)

CREATE TABLE users (
    id UUID NOT NULL, 
    actor_id TEXT NOT NULL, 
    display_name TEXT NOT NULL, 
    email TEXT NOT NULL, 
    auth_subject TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_users PRIMARY KEY (id), 
    CONSTRAINT uq_users_actor_id UNIQUE (actor_id)
);

CREATE UNIQUE INDEX ix_users_auth_subject ON users (auth_subject) WHERE auth_subject IS NOT NULL;

CREATE INDEX ix_users_email_lower ON users (lower(email));

CREATE TABLE projects (
    id UUID NOT NULL, 
    name TEXT NOT NULL, 
    created_by UUID NOT NULL, 
    pinned_commit_sha TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    removed_files INTEGER DEFAULT 0 NOT NULL, 
    CONSTRAINT pk_projects PRIMARY KEY (id), 
    CONSTRAINT ck_projects_name_not_blank CHECK (name !~ '^\s*$'), 
    CONSTRAINT fk_projects_created_by_users FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE INDEX ix_projects_created_by ON projects (created_by);

CREATE TABLE project_members (
    project_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    role TEXT NOT NULL, 
    bio TEXT, 
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_project_members PRIMARY KEY (project_id, user_id), 
    CONSTRAINT ck_project_members_role CHECK (role IN ('owner', 'collaborator')), 
    CONSTRAINT fk_project_members_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id), 
    CONSTRAINT fk_project_members_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_project_members_user_id ON project_members (user_id);

CREATE UNIQUE INDEX ux_project_members_one_owner ON project_members (project_id) WHERE role = 'owner';

CREATE TABLE project_invites (
    id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    email_norm TEXT NOT NULL, 
    invited_by UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_project_invites PRIMARY KEY (id), 
    CONSTRAINT ck_project_invites_email_norm CHECK (email_norm = lower(btrim(email_norm))), 
    CONSTRAINT fk_project_invites_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id), 
    CONSTRAINT fk_project_invites_invited_by_users FOREIGN KEY(invited_by) REFERENCES users (id), 
    CONSTRAINT uq_project_invites_project_id_email_norm UNIQUE (project_id, email_norm)
);

CREATE INDEX ix_project_invites_email_norm ON project_invites (email_norm);

CREATE TABLE connections (
    id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    kind TEXT NOT NULL, 
    external_id TEXT NOT NULL, 
    link TEXT, 
    name TEXT NOT NULL, 
    state TEXT DEFAULT 'listing' NOT NULL, 
    failure_reason TEXT, 
    connected_by UUID NOT NULL, 
    last_synced_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    expected_files INTEGER, 
    ended_at TIMESTAMP WITH TIME ZONE, 
    name_suffix INTEGER DEFAULT 1 NOT NULL, 
    CONSTRAINT pk_connections PRIMARY KEY (id), 
    CONSTRAINT ck_connections_kind CHECK (kind IN ('gdrive', 'upload')), 
    CONSTRAINT ck_connections_external_id CHECK (external_id ~ '^[A-Za-z0-9_-]{10,}$'), 
    CONSTRAINT ck_connections_state CHECK (state IN ('listing', 'importing', 'done', 'failed')), 
    CONSTRAINT ck_connections_failure_reason CHECK (failure_reason IN ('invalid_link', 'no_access', 'not_a_folder', 'already_connected', 'drive_unavailable', 'setup_error', 'too_slow', 'unknown_error')), 
    CONSTRAINT ck_connections_failure_reason_matches_state CHECK ((state = 'failed') = (failure_reason IS NOT NULL)), 
    CONSTRAINT ck_connections_link_for_drive CHECK (kind <> 'gdrive' OR link IS NOT NULL), 
    CONSTRAINT ck_connections_expected_files CHECK (expected_files IS NULL OR expected_files BETWEEN 1 AND 10000), 
    CONSTRAINT ck_connections_name_suffix CHECK (name_suffix >= 1), 
    CONSTRAINT fk_connections_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id), 
    CONSTRAINT fk_connections_connected_by_users FOREIGN KEY(connected_by) REFERENCES users (id), 
    CONSTRAINT uq_connections_project_kind_external UNIQUE (project_id, kind, external_id)
);

CREATE INDEX ix_connections_project_id ON connections (project_id);

CREATE TABLE documents (
    id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    content_hash TEXT NOT NULL, 
    original_filename TEXT NOT NULL, 
    media_type TEXT NOT NULL, 
    size_bytes BIGINT NOT NULL, 
    origin TEXT NOT NULL, 
    original_key TEXT NOT NULL, 
    markdown_key TEXT, 
    parser TEXT, 
    version INTEGER DEFAULT 1 NOT NULL, 
    previous_revision_id UUID, 
    status TEXT DEFAULT 'uploaded' NOT NULL, 
    error TEXT, 
    skipped_fragments INTEGER DEFAULT 0 NOT NULL, 
    uploaded_by UUID, 
    markdown TEXT, 
    summarization TEXT, 
    summarization_status TEXT DEFAULT 'none' NOT NULL, 
    summarization_model TEXT, 
    summarization_error TEXT, 
    summarized_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    source_id UUID, 
    CONSTRAINT pk_documents PRIMARY KEY (id), 
    CONSTRAINT ck_documents_status CHECK (status IN ('uploaded', 'processing', 'parsed', 'failed', 'ingested', 'partially_ingested')), 
    CONSTRAINT ck_documents_summarization_status CHECK (summarization_status IN ('none', 'running', 'succeeded', 'failed')), 
    CONSTRAINT ck_documents_origin CHECK (origin IN ('upload', 'connection', 'todo_answer')), 
    CONSTRAINT ck_documents_content_hash_sha256 CHECK (content_hash ~ '^[0-9a-f]{64}$'), 
    CONSTRAINT ck_documents_previous_revision CHECK ((previous_revision_id IS NULL) = (version = 1)), 
    CONSTRAINT fk_documents_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id), 
    CONSTRAINT fk_documents_previous_revision_id_documents FOREIGN KEY(previous_revision_id) REFERENCES documents (id), 
    CONSTRAINT fk_documents_uploaded_by_users FOREIGN KEY(uploaded_by) REFERENCES users (id), 
    CONSTRAINT fk_documents_source_id_connections FOREIGN KEY(source_id) REFERENCES connections (id), 
    CONSTRAINT uq_documents_project_id_content_hash UNIQUE (project_id, content_hash)
);

CREATE INDEX ix_documents_project_id_created_at ON documents (project_id, created_at DESC);

CREATE INDEX ix_documents_project_id_status ON documents (project_id, status);

CREATE INDEX ix_documents_source_id ON documents (source_id);

CREATE INDEX ix_documents_unheld ON documents (project_id) WHERE source_id IS NULL AND origin IN ('upload', 'connection');

CREATE TABLE connection_items (
    id UUID NOT NULL, 
    connection_id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    external_id TEXT NOT NULL, 
    name TEXT NOT NULL, 
    mime_type TEXT NOT NULL, 
    category TEXT NOT NULL, 
    processing_class TEXT NOT NULL, 
    size_bytes BIGINT, 
    modified_time TIMESTAMP WITH TIME ZONE, 
    drive_version BIGINT, 
    md5 TEXT, 
    state TEXT DEFAULT 'pending' NOT NULL, 
    error_reason TEXT, 
    error_step TEXT, 
    document_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_connection_items PRIMARY KEY (id), 
    CONSTRAINT ck_connection_items_category CHECK (category IN ('google_docs', 'google_slides', 'google_sheets', 'pdf', 'word', 'powerpoint', 'excel', 'images', 'videos', 'other_files', 'other_google', 'shortcuts')), 
    CONSTRAINT ck_connection_items_processing_class CHECK (processing_class IN ('processed', 'stored_only', 'skipped')), 
    CONSTRAINT ck_connection_items_state CHECK (state IN ('pending', 'processing', 'processed', 'stored', 'skipped', 'error', 'duplicate')), 
    CONSTRAINT ck_connection_items_error_reason CHECK (error_reason IN ('no_access', 'drive_unavailable', 'convert_failed', 'parse_failed', 'store_failed', 'import_failed', 'unknown_error')), 
    CONSTRAINT ck_connection_items_error_step CHECK (error_step IN ('list', 'download', 'read', 'convert', 'store', 'parse')), 
    CONSTRAINT ck_connection_items_error_reason_matches_state CHECK ((state = 'error') = (error_reason IS NOT NULL)), 
    CONSTRAINT ck_connection_items_duplicate_has_document CHECK (state <> 'duplicate' OR document_id IS NOT NULL), 
    CONSTRAINT fk_connection_items_connection_id_connections FOREIGN KEY(connection_id) REFERENCES connections (id), 
    CONSTRAINT fk_connection_items_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id), 
    CONSTRAINT fk_connection_items_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id), 
    CONSTRAINT uq_connection_items_connection_external UNIQUE (connection_id, external_id)
);

CREATE INDEX ix_connection_items_connection_id_state ON connection_items (connection_id, state);

CREATE INDEX ix_connection_items_project_id ON connection_items (project_id);

CREATE INDEX ix_connection_items_document_id ON connection_items (document_id);

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_project_scope ON projects TO "specflow_agent" USING (id = NULLIF(current_setting('app.project_id', true), '')::uuid) WITH CHECK (id = NULLIF(current_setting('app.project_id', true), '')::uuid);

ALTER TABLE project_members ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_project_scope ON project_members TO "specflow_agent" USING (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid) WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid);

ALTER TABLE project_invites ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_project_scope ON project_invites TO "specflow_agent" USING (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid) WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_project_scope ON documents TO "specflow_agent" USING (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid) WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid);

ALTER TABLE connections ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_project_scope ON connections TO "specflow_agent" USING (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid) WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid);

ALTER TABLE connection_items ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_project_scope ON connection_items TO "specflow_agent" USING (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid) WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT (id, project_id, original_filename, version, status, markdown) ON documents TO "specflow_agent";
