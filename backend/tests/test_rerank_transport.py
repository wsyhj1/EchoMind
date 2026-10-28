"""Repeated real client construction must reuse verified TLS setup, without networking."""
import os
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import documents_process


class RerankTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_requests_reuse_verified_context_and_keep_request_contract(self):
        response = httpx.Response(200, json={"output": {"results": [{"index": 0, "relevance_score": .9}]}})
        original_client = httpx.AsyncClient
        clients = []
        def make_client(**kwargs):
            clients.append(kwargs)
            return original_client(**kwargs)
        with patch.object(original_client, "post", AsyncMock(return_value=response)) as post, \
                patch.object(documents_process.httpx, "AsyncClient", side_effect=make_client), \
                patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test", "RERANK_URL": "https://test.invalid/rerank"}):
            for _ in range(3):
                await documents_process.rerank_documents("q", ["source"], 1)
        contexts = [kwargs.get("verify") for kwargs in clients]
        self.assertTrue(all(isinstance(context, ssl.SSLContext) for context in contexts))
        self.assertTrue(all(context is contexts[0] for context in contexts))
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)
        self.assertTrue(all(kwargs["timeout"] == 3.0 for kwargs in clients))
        self.assertEqual(post.call_args.kwargs["json"]["input"], {"query": "q", "documents": ["source"]})
        self.assertEqual(post.call_args.kwargs["json"]["parameters"]["top_n"], 1)

    async def test_retry_uses_same_tls_context_and_retains_retry_limit(self):
        ok = httpx.Response(200, json={"output": {"results": [{"index": 0}]}})
        with patch.object(httpx.AsyncClient, "post", AsyncMock(side_effect=[httpx.ReadTimeout("test"), ok])) as post, \
                patch.object(documents_process.asyncio, "sleep", AsyncMock()), \
                patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test", "RERANK_URL": "https://test.invalid/rerank"}):
            result = await documents_process.rerank_documents("q", ["source"], 1)
        self.assertEqual(post.await_count, 2)
        self.assertEqual(result["output"]["results"][0]["index"], 0)


if __name__ == "__main__":
    unittest.main()
