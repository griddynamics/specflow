CREATE INDEX IF NOT EXISTS ix_users_email_lower ON users (lower(email))
