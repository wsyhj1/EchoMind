-- Additive migration: old runs, raw messages and checkpoint keys remain intact.
CREATE TABLE IF NOT EXISTS chat_conversations (
    conversation_id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    session_id VARCHAR(64) NOT NULL,
    client_create_id UUID NOT NULL,
    scope_id TEXT NOT NULL,
    title VARCHAR(160) NOT NULL DEFAULT '新对话',
    archived BOOLEAN NOT NULL DEFAULT false,
    read_only BOOLEAN NOT NULL DEFAULT false,
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, session_id),
    UNIQUE(user_id, client_create_id)
);
CREATE INDEX IF NOT EXISTS chat_conversations_directory
    ON chat_conversations(user_id, scope_id, archived, updated_at DESC, conversation_id DESC);

CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES chat_conversations(conversation_id) ON DELETE CASCADE,
    client_turn_id UUID NOT NULL,
    sequence BIGINT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    user_text TEXT NOT NULL,
    model_query TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed','cancelled')),
    run_id UUID NOT NULL UNIQUE REFERENCES evaluation_runs(run_id) ON DELETE RESTRICT,
    settings JSONB NOT NULL,
    scope_snapshot JSONB NOT NULL,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE(conversation_id, client_turn_id),
    UNIQUE(conversation_id, sequence)
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_turns_one_active
    ON chat_turns(conversation_id) WHERE status='running';
