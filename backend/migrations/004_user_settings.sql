-- Preferences belong to the user, independently of profile content/revisions.
ALTER TABLE users ADD COLUMN IF NOT EXISTS app_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS app_settings_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS app_settings_updated_at TIMESTAMPTZ;
