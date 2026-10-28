-- One-shot local reminders. Delivery and inbox insertion share a transaction.
CREATE TABLE IF NOT EXISTS assistant_reminders (
    reminder_id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL,
    project_id UUID NOT NULL,
    task_id UUID REFERENCES assistant_tasks(task_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','delivered','cancelled')),
    reason TEXT NOT NULL DEFAULT '',
    revision BIGINT NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(user_id,project_id) REFERENCES assistant_projects(user_id,project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS assistant_reminders_due ON assistant_reminders(due_at,project_id)
    WHERE status='scheduled';
CREATE INDEX IF NOT EXISTS assistant_reminders_project ON assistant_reminders(project_id,created_at DESC,reminder_id);
CREATE TABLE IF NOT EXISTS assistant_notifications (
    notification_id UUID PRIMARY KEY,
    reminder_id UUID NOT NULL REFERENCES assistant_reminders(reminder_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    title TEXT NOT NULL,
    project_title TEXT NOT NULL,
    task_title TEXT,
    due_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    UNIQUE(reminder_id,generation)
);
CREATE TABLE IF NOT EXISTS assistant_reminder_operations (
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    operation_id UUID NOT NULL,
    fingerprint TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id,operation_id)
);
CREATE INDEX IF NOT EXISTS assistant_notifications_unread ON assistant_notifications(delivered_at DESC,notification_id DESC)
    WHERE read_at IS NULL;
CREATE TABLE IF NOT EXISTS assistant_scheduler_state (
    name TEXT PRIMARY KEY,
    last_tick_at TIMESTAMPTZ NOT NULL
);
