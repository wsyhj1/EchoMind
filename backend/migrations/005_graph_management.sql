-- Additive graph management/checkpoint migration. Never modify applied 003.
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS explicit_requested BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS consented_at TIMESTAMPTZ;
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS consent_config_sha256 TEXT;
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS consent_host TEXT;
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS consent_model TEXT;
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS generation INT NOT NULL DEFAULT 1 CHECK(generation>0);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS parent_limit INT NOT NULL DEFAULT 200 CHECK(parent_limit BETWEEN 1 AND 200);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS call_limit INT NOT NULL DEFAULT 400 CHECK(call_limit BETWEEN 1 AND 400);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS model_calls INT NOT NULL DEFAULT 0 CHECK(model_calls>=0);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS lifetime_model_calls INT NOT NULL DEFAULT 0 CHECK(lifetime_model_calls>=0);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS parents_total INT NOT NULL DEFAULT 0 CHECK(parents_total>=0);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS parents_completed INT NOT NULL DEFAULT 0 CHECK(parents_completed>=0);
ALTER TABLE kg_index_jobs ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS kg_jobs_explicit_pending ON kg_index_jobs(explicit_requested,status,created_at);
CREATE TABLE IF NOT EXISTS kg_parent_checkpoints (
 job_id UUID NOT NULL,parent_id VARCHAR(128) NOT NULL,user_id INT NOT NULL,
 knowledge_base_id VARCHAR(128) NOT NULL,file_hash VARCHAR(64) NOT NULL,extraction_version VARCHAR(32) NOT NULL,
 generation INT NOT NULL,lease_id UUID NOT NULL,source_sha256 VARCHAR(64) NOT NULL,config_sha256 VARCHAR(64) NOT NULL,
 extraction JSONB NOT NULL,rejected_relations INT NOT NULL CHECK(rejected_relations>=0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(job_id,parent_id,generation),
 FOREIGN KEY(job_id,user_id,knowledge_base_id,file_hash,extraction_version)
 REFERENCES kg_index_jobs(job_id,user_id,knowledge_base_id,file_hash,extraction_version) ON DELETE CASCADE,
 FOREIGN KEY(parent_id,knowledge_base_id,user_id,file_hash)
 REFERENCES parent_chunks(parent_id,knowledge_base_id,user_id,file_hash) ON DELETE CASCADE
);
