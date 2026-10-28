-- Additive, repeatable; the original conversations/documents are not rewritten.
CREATE TABLE IF NOT EXISTS assistant_projects (
    project_id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    client_create_id UUID NOT NULL,
    create_fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    scope_id TEXT NOT NULL,
    archived BOOLEAN NOT NULL DEFAULT false,
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,client_create_id),
    UNIQUE(user_id,project_id)
);
CREATE INDEX IF NOT EXISTS assistant_projects_directory
    ON assistant_projects(user_id,archived,updated_at DESC,project_id);

CREATE TABLE IF NOT EXISTS assistant_tasks (
    task_id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES assistant_projects(project_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL CHECK(category IN ('work','study')),
    priority TEXT NOT NULL CHECK(priority IN ('high','normal','low')),
    status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','in_progress','blocked','done','cancelled')),
    estimated_minutes INTEGER CHECK(estimated_minutes BETWEEN 5 AND 1440),
    due_date DATE,
    completion_note TEXT NOT NULL DEFAULT '',
    blocker TEXT NOT NULL DEFAULT '',
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assistant_tasks_project ON assistant_tasks(project_id,status,due_date);

CREATE TABLE IF NOT EXISTS assistant_notes (
    note_id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES assistant_projects(project_id) ON DELETE CASCADE,
    note_kind TEXT NOT NULL CHECK(note_kind IN ('progress','decision','blocker','learning')),
    content TEXT NOT NULL,
    superseded_by UUID REFERENCES assistant_notes(note_id),
    source TEXT NOT NULL,
    source_turn_id UUID REFERENCES chat_turns(turn_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assistant_resources (
    resource_id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES assistant_projects(project_id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('document','link')),
    label TEXT NOT NULL,
    reference TEXT NOT NULL,
    knowledge_base_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,resource_type,reference)
);
CREATE TABLE IF NOT EXISTS assistant_project_conversations (
    conversation_id UUID PRIMARY KEY REFERENCES chat_conversations(conversation_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    project_id UUID NOT NULL,
    FOREIGN KEY(user_id,project_id) REFERENCES assistant_projects(user_id,project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assistant_events (
    event_id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES assistant_projects(project_id) ON DELETE CASCADE,
    revision BIGINT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('user_form','user_command','assistant_proposal')),
    source_turn_id UUID REFERENCES chat_turns(turn_id) ON DELETE RESTRICT,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,revision)
);
CREATE INDEX IF NOT EXISTS assistant_events_history ON assistant_events(project_id,created_at DESC,revision DESC);
CREATE TABLE IF NOT EXISTS assistant_operations (
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    operation_id UUID NOT NULL,
    project_id UUID NOT NULL REFERENCES assistant_projects(project_id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id,operation_id)
);
CREATE TABLE IF NOT EXISTS assistant_proposals (
    proposal_id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL,
    project_id UUID NOT NULL,
    request_key UUID NOT NULL,
    fingerprint TEXT NOT NULL,
    base_revision BIGINT NOT NULL,
    action JSONB NOT NULL,
    source_turn_id UUID NOT NULL REFERENCES chat_turns(turn_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','rejected')),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    UNIQUE(user_id,request_key),
    FOREIGN KEY(user_id,project_id) REFERENCES assistant_projects(user_id,project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS assistant_proposals_pending ON assistant_proposals(project_id,status,created_at DESC);
