-- Extends the original predicate allowlist without changing migration 003.
-- Entity types have always been varchar(64); no entity CHECK needs replacing.
ALTER TABLE kg_relations DROP CONSTRAINT IF EXISTS kg_relations_predicate_check;
ALTER TABLE kg_relations ADD CONSTRAINT kg_relations_predicate_check CHECK(predicate IN
 ('USES','DEPENDS_ON','MAINTAINED_BY','DEVELOPED_BY','HAS_ISSUE','RESOLVED_BY','PART_OF',
  'HAS_FEATURE','HAS_MODULE','IMPLEMENTED_BY','EVALUATED_ON','REPORTS_METRIC','HAS_EXPERIMENT'));
