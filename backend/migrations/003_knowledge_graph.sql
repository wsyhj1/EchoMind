-- Executed by GraphRepository.initialize under a transaction/advisory lock.
CREATE UNIQUE INDEX IF NOT EXISTS kg_parent_source_key ON parent_chunks(parent_id,knowledge_base_id,user_id,file_hash);
CREATE TABLE IF NOT EXISTS kg_index_jobs (
 job_id UUID PRIMARY KEY, user_id INT NOT NULL, knowledge_base_id VARCHAR(128) NOT NULL,
 file_hash VARCHAR(64) NOT NULL, extraction_version VARCHAR(32) NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','READY','FAILED','STALE')),
 retry_count INT NOT NULL DEFAULT 0, lease_id UUID, locked_until TIMESTAMPTZ,
 last_error TEXT, stats JSONB NOT NULL DEFAULT '{}', started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(user_id,knowledge_base_id,file_hash,extraction_version),
 UNIQUE(job_id,user_id,knowledge_base_id,file_hash,extraction_version),
 FOREIGN KEY(file_hash,knowledge_base_id,user_id) REFERENCES file_metadata ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS kg_jobs_pending ON kg_index_jobs(status,locked_until,created_at);
CREATE TABLE IF NOT EXISTS kg_entities (
 entity_id UUID PRIMARY KEY, user_id INT NOT NULL, knowledge_base_id VARCHAR(128) NOT NULL,
 entity_type VARCHAR(64) NOT NULL, canonical_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 qualifier TEXT NOT NULL DEFAULT '', aliases JSONB NOT NULL DEFAULT '[]',
 UNIQUE(entity_id,user_id,knowledge_base_id),
 UNIQUE(user_id,knowledge_base_id,entity_type,normalized_name,qualifier),
 FOREIGN KEY(knowledge_base_id,user_id) REFERENCES knowledge_bases ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS kg_entity_lookup ON kg_entities(user_id,knowledge_base_id,normalized_name);
CREATE TABLE IF NOT EXISTS kg_relations (
 relation_id UUID PRIMARY KEY, user_id INT NOT NULL, knowledge_base_id VARCHAR(128) NOT NULL,
 subject_id UUID NOT NULL, predicate TEXT NOT NULL CHECK(predicate IN
 ('USES','DEPENDS_ON','MAINTAINED_BY','DEVELOPED_BY','HAS_ISSUE','RESOLVED_BY','PART_OF')),
 object_id UUID NOT NULL, extraction_version VARCHAR(32) NOT NULL,
 UNIQUE(relation_id,user_id,knowledge_base_id,extraction_version),
 UNIQUE(user_id,knowledge_base_id,subject_id,predicate,object_id,extraction_version),
 FOREIGN KEY(subject_id,user_id,knowledge_base_id) REFERENCES kg_entities(entity_id,user_id,knowledge_base_id) ON DELETE CASCADE,
 FOREIGN KEY(object_id,user_id,knowledge_base_id) REFERENCES kg_entities(entity_id,user_id,knowledge_base_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS kg_rel_outgoing ON kg_relations(user_id,knowledge_base_id,subject_id,predicate);
CREATE TABLE IF NOT EXISTS kg_relation_evidence (
 evidence_id UUID PRIMARY KEY, relation_id UUID NOT NULL, user_id INT NOT NULL,
 knowledge_base_id VARCHAR(128) NOT NULL, parent_id VARCHAR(128) NOT NULL, file_hash VARCHAR(64) NOT NULL,
 quote TEXT NOT NULL, evidence_hash VARCHAR(64) NOT NULL, extraction_version VARCHAR(32) NOT NULL, job_id UUID NOT NULL,
 UNIQUE(relation_id,parent_id,evidence_hash),
 FOREIGN KEY(relation_id,user_id,knowledge_base_id,extraction_version)
 REFERENCES kg_relations(relation_id,user_id,knowledge_base_id,extraction_version) ON DELETE CASCADE,
 FOREIGN KEY(parent_id,knowledge_base_id,user_id,file_hash) REFERENCES parent_chunks(parent_id,knowledge_base_id,user_id,file_hash) ON DELETE CASCADE,
 FOREIGN KEY(job_id,user_id,knowledge_base_id,file_hash,extraction_version)
 REFERENCES kg_index_jobs(job_id,user_id,knowledge_base_id,file_hash,extraction_version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS kg_evidence_rel ON kg_relation_evidence(relation_id);
CREATE TABLE IF NOT EXISTS kg_entity_mentions (
 entity_id UUID NOT NULL, parent_id VARCHAR(128) NOT NULL, user_id INT NOT NULL,
 knowledge_base_id VARCHAR(128) NOT NULL, file_hash VARCHAR(64) NOT NULL,
 extraction_version VARCHAR(32) NOT NULL, job_id UUID NOT NULL,
 PRIMARY KEY(entity_id,parent_id,user_id,knowledge_base_id,extraction_version),
 FOREIGN KEY(entity_id,user_id,knowledge_base_id) REFERENCES kg_entities(entity_id,user_id,knowledge_base_id) ON DELETE CASCADE,
 FOREIGN KEY(parent_id,knowledge_base_id,user_id,file_hash) REFERENCES parent_chunks(parent_id,knowledge_base_id,user_id,file_hash) ON DELETE CASCADE,
 FOREIGN KEY(job_id,user_id,knowledge_base_id,file_hash,extraction_version)
 REFERENCES kg_index_jobs(job_id,user_id,knowledge_base_id,file_hash,extraction_version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS kg_mentions_parent ON kg_entity_mentions(user_id,knowledge_base_id,parent_id);
-- Independent of deleted files/KBs, so a committed PG delete retains its compensation task.
CREATE TABLE IF NOT EXISTS kb_delete_tasks (
 task_id UUID PRIMARY KEY, user_id INT NOT NULL, knowledge_base_id VARCHAR(128) NOT NULL,
 file_hash VARCHAR(64) NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INT NOT NULL DEFAULT 0, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(user_id,knowledge_base_id,file_hash)
);
