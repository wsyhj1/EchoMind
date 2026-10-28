CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id VARCHAR(80) NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evaluation_runs_session ON evaluation_runs(user_id,session_id,created_at DESC);
CREATE TABLE IF NOT EXISTS evaluation_jobs (
    job_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until TIMESTAMPTZ,
    lease_id UUID,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evaluation_jobs_queue ON evaluation_jobs(status,available_at);
