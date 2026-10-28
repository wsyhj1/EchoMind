CREATE TABLE IF NOT EXISTS citation_validation_snapshots (
    validation_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS citation_validation_owner_run
    ON citation_validation_snapshots(user_id,run_id,checked_at DESC);
