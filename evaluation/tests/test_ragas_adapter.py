"""Real RAGAS/Instructor/OpenAI integration over an in-memory HTTP transport."""
import json
import os
import unittest

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

import httpx
from pydantic import BaseModel
from evaluation.engine import RagasEvaluator


class Verdict(BaseModel):
    verdict: int


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_output_parameters_and_embeddings_use_compatible_wire_format(self):
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            if request.url.path.endswith("embeddings"):
                return httpx.Response(200, json={"object": "list", "model": "test-embedding",
                    "data": [{"object": "embedding", "index": 0, "embedding": [1., 0.]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1}})
            self.assertNotIn("mode", body, "Instructor mode must never be forwarded as a model API parameter")
            self.assertEqual(body["response_format"]["type"], "json_object")
            self.assertIs(body["enable_thinking"], False)
            return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
                "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"verdict":1}'}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})

        evaluator = RagasEvaluator({"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.5-flash",
                                    "embedding_model": "test-embedding"}, "test", transport=httpx.MockTransport(handle))
        try:
            await evaluator._initialize()
            result = await evaluator.metrics["faithfulness"].llm.agenerate("Return verdict 1", Verdict)
            self.assertEqual(result.verdict, 1)
            vector = await evaluator.metrics["response_relevancy"].embeddings.aembed_text("a")
            self.assertEqual(vector, [1., 0.])
            self.assertEqual(len(requests), 2)
            self.assertEqual(evaluator.usage["prompt_tokens"], 2)
            self.assertEqual(evaluator.usage["completion_tokens"], 3)
            self.assertEqual(evaluator.usage["embedding_tokens"], 1)
        finally:
            if evaluator.client:
                await evaluator.client.close()


if __name__ == "__main__":
    unittest.main()
