"""Persisted actual-prompt citations and live tenant-scoped source checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citation_service import CitationService, parse_citations


def run_fixture():
    context = {"stage": "final", "source_type": "knowledge", "included_in_prompt": True,
               "text": '真实原文\n<script>alert("unsafe")</script>', "parent_id": "p1",
               "knowledge_base_id": "kb", "user_id": 1, "file_hash": "hash", "file_name": "notes.pdf"}
    payload = {"evidence": [{"id": "E1", "file": "notes.pdf", "knowledge_base": "kb", "text": context["text"]}]}
    return {"run_id": str(uuid4()), "user_id": 1, "knowledge_base_id": "kb",
            "answer": "没有依据的额外结论（来源：notes.pdf，E1）", "contexts": [context],
            "model_inputs": [{"messages": [{"role": "tool", "content": "不可信引文。\n" + json.dumps(payload, ensure_ascii=False)}]}]}


class CitationValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.run = run_fixture()
        self.pool = AsyncMock()
        self.pool.fetchrow.return_value = {"text": self.run["contexts"][0]["text"], "file_name": "notes.pdf", "file_hash": "hash"}
        self.pool.fetchval.return_value = False
        self.store = AsyncMock()
        self.store.get_run.return_value = self.run
        self.service = CitationService(self.store, self.pool)

    async def validate(self, citation=None):
        return await self.service.validate(self.run["run_id"], 1, include_citation=citation)

    async def test_exact_pair_and_escaped_newline_are_structural_not_semantic_success(self):
        snapshot = await self.validate()
        self.assertEqual(snapshot["citations"][0]["status"], "structure_verified")
        self.assertEqual(snapshot["citations"][0]["semantic_status"], "unverified")
        self.assertEqual(snapshot["statistics"]["semantic_verified"], 0)
        self.assertNotIn("真实原文", self.pool.execute.call_args.args[-1])
        detail = await self.validate("c1")
        self.assertEqual(detail["text"], self.run["contexts"][0]["text"])
        self.assertIn("p.user_id=$1", self.pool.fetchrow.call_args.args[0])
        self.assertEqual(self.pool.fetchrow.call_args.args[1:], (1, "kb", "p1", "hash"))

    async def test_unicode_line_separators_inside_json_do_not_hide_actual_evidence(self):
        for separator in ("\u2028", "\u2029", "\u0085"):
            text = f"第一句{separator}第二句"
            self.run["contexts"][0]["text"] = text
            self.pool.fetchrow.return_value = {"text": text, "file_name": "notes.pdf", "file_hash": "hash"}
            payload = {"evidence": [{"id": "E1", "file": "notes.pdf", "knowledge_base": "kb", "text": text}]}
            self.run["model_inputs"][-1]["messages"][0]["content"] = "引文。\n" + json.dumps(payload, ensure_ascii=False)
            detail = await self.validate("c1")
            self.assertEqual(detail["citation"]["status"], "structure_verified")
            self.assertEqual(detail["text"], text)

    async def test_wrong_file_evidence_pair_is_never_replaced_with_guess(self):
        self.run["answer"] = "结论（来源：other.pdf，E1）"
        result = await self.validate("c1")
        self.assertEqual(result["citation"]["status"], "pair_mismatch")
        self.assertEqual(result["citation"]["file"], "other.pdf")
        self.assertIsNone(result["text"])
        self.pool.fetchrow.assert_not_awaited()

    async def test_deleted_and_changed_sources_do_not_reveal_historical_text(self):
        self.pool.fetchrow.return_value = None
        self.assertEqual((await self.validate("c1"))["citation"]["status"], "source_deleted")
        self.assertIsNone((await self.validate("c1"))["text"])
        self.pool.fetchrow.return_value = {"text": "新原文", "file_name": "notes.pdf"}
        self.assertEqual((await self.validate("c1"))["citation"]["status"], "source_changed")
        self.assertIsNone((await self.validate("c1"))["text"])

    async def test_pending_delete_hides_source_before_physical_cleanup(self):
        self.pool.fetchval.return_value = True
        result = await self.validate("c1")
        self.assertEqual(result["citation"]["status"], "deletion_pending")
        self.assertIsNone(result["text"])

    async def test_other_user_run_cannot_validate_or_fetch_source(self):
        with self.assertRaises(LookupError):
            await self.service.validate(self.run["run_id"], 2, include_citation="c1")
        self.pool.fetchrow.assert_not_awaited()
        self.pool.execute.assert_not_awaited()

    async def test_other_kb_or_missing_final_model_visibility_stays_unverified(self):
        self.run["knowledge_base_id"] = "other"
        self.assertEqual((await self.validate("c1"))["citation"]["status"], "unverified")
        self.pool.fetchrow.assert_not_awaited()
        self.run["knowledge_base_id"] = "kb"
        self.run["model_inputs"].append({"messages": [{"role": "human", "content": "no tools"}]})
        self.assertEqual((await self.validate("c1"))["citation"]["status"], "unverified")

    async def test_same_label_file_two_parents_is_ambiguous(self):
        other = deepcopy(self.run["contexts"][0]); other["parent_id"] = "p2"
        self.run["contexts"].append(other)
        self.assertEqual((await self.validate("c1"))["citation"]["status"], "ambiguous")
        self.pool.fetchrow.assert_not_awaited()

    async def test_multiple_citations_share_one_live_lookup_only_within_this_check(self):
        self.run["answer"] += " 另句（来源：notes.pdf，E1）"
        self.assertEqual((await self.validate())["statistics"]["citation_occurrences"], 2)
        self.assertEqual(self.pool.fetchrow.await_count, 1)
        await self.validate()
        self.assertEqual(self.pool.fetchrow.await_count, 2)

    async def test_cache_does_not_share_verdict_across_renamed_file_or_foreign_context(self):
        original = self.run["contexts"][0]
        old = {**original, "file_name": "old.pdf"}
        foreign = {**original, "user_id": 2, "file_name": "foreign.pdf"}
        self.run["contexts"] += [old, foreign]
        records = [{"id": eid, "file": c["file_name"], "knowledge_base": "kb", "text": c["text"]}
                   for eid, c in zip(("E1", "E2", "E3"), self.run["contexts"])]
        self.run["model_inputs"][-1]["messages"][0]["content"] = json.dumps({"evidence": records})
        self.run["answer"] = "甲（来源：notes.pdf，E1；old.pdf，E2；foreign.pdf，E3）"
        snapshot = await self.validate()
        self.assertEqual([r["status"] for r in snapshot["citations"]],
                         ["structure_verified", "source_changed", "unverified"])

    def test_parser_handles_multiple_pairs_book_titles_missing_fields_and_code(self):
        refs = parse_citations('甲（来源：甲.pdf，E1；乙.docx，E2） 乙《丙.pdf》（E3） 丙见E4。'
                               '（来源：无编号.pdf） `E8` ```\nE9\n```')
        self.assertEqual([(r["file"], r["evidence_id"]) for r in refs],
                         [("甲.pdf", "E1"), ("乙.docx", "E2"), ("丙.pdf", "E3"), (None, "E4"), ("无编号.pdf", None)])

    async def test_router_requires_identity_and_matches_user_before_loading_run(self):
        import httpx
        from fastapi import FastAPI
        from citations_api import router
        app = FastAPI()
        app.include_router(router)
        app.state.citation_service = self.service
        @app.middleware("http")
        async def identity(request, call_next):
            if request.headers.get("x-test-identity"):
                request.state.identity = SimpleNamespace(user_id=1)
            return await call_next(request)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            url = f"/runs/{self.run['run_id']}/citations"
            self.assertEqual((await client.get(url, params={"user_id": 1})).status_code, 401)
            self.assertEqual((await client.get(url, params={"user_id": 2}, headers={"x-test-identity": "yes"})).status_code, 403)
            self.store.get_run.assert_not_awaited()
            result = await client.get(url, params={"user_id": 1}, headers={"x-test-identity": "yes"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["semantic_validation"], "not_performed")


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Set ECHOMIND_TEST_DATABASE_URL for isolated PostgreSQL checks")
class CitationStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        from evaluation_store import EvaluationStore
        self.schema = "citation_test_" + uuid4().hex
        self.admin = await asyncpg.connect(os.environ["ECHOMIND_TEST_DATABASE_URL"])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ["ECHOMIND_TEST_DATABASE_URL"], min_size=1, max_size=2,
                                            server_settings={"search_path": self.schema})
        self.store = EvaluationStore(self.pool)
        await self.store.initialize()
        await self.pool.execute("""CREATE TABLE knowledge_bases(user_id INT, knowledge_base_id TEXT);
            CREATE TABLE file_metadata(user_id INT,knowledge_base_id TEXT,file_hash TEXT,file_name TEXT);
            CREATE TABLE parent_chunks(user_id INT,knowledge_base_id TEXT,parent_id TEXT,file_hash TEXT,file_name TEXT,text TEXT);""")
        self.run = run_fixture()
        self.run.update(session_id="unit", status="complete")
        await self.store.save_run(self.run)
        await self.pool.execute("INSERT INTO knowledge_bases VALUES(1,'kb')")
        await self.pool.execute("INSERT INTO file_metadata VALUES(1,'kb','hash','notes.pdf')")
        await self.pool.execute("INSERT INTO parent_chunks VALUES(1,'kb','p1','hash','notes.pdf',$1)", self.run["contexts"][0]["text"])
        self.service = CitationService(self.store, self.pool)
        await self.service.initialize()
        await self.service.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        if not self.schema.startswith("citation_test_") or not self.schema.removeprefix("citation_test_").isalnum():
            raise RuntimeError("Unsafe test schema")
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def test_persisted_audit_and_deleted_file_are_isolated(self):
        first = await self.service.validate(self.run["run_id"], 1, include_citation="c1")
        self.assertEqual(first["text"], self.run["contexts"][0]["text"])
        audit = await self.pool.fetchval("SELECT snapshot FROM citation_validation_snapshots")
        self.assertNotIn("真实原文", audit)
        await self.pool.execute("DELETE FROM file_metadata WHERE user_id=1")
        deleted = await self.service.validate(self.run["run_id"], 1, include_citation="c1")
        self.assertEqual(deleted["citation"]["status"], "source_deleted")
        self.assertIsNone(deleted["text"])
        with self.assertRaises(LookupError):
            await self.service.validate(self.run["run_id"], 2, include_citation="c1")


if __name__ == "__main__":
    unittest.main()
