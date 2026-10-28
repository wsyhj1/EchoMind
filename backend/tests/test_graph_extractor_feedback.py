"""Real extractor-seam regression for a captured schema-metadata response pattern."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.extractor import GraphExtractor
from graph.settings import GraphSettings
from pydantic import ValidationError


def response(data):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))])


class GraphExtractorFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_metadata_gets_bounded_private_safe_feedback_then_valid_data(self):
        invalid = {'entities': [], 'relations': [], '$defs': {'private': 'DO_NOT_ECHO_PRIVATE_INPUT'}}
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return response(invalid)
            feedback = kwargs['messages'][-1]['content']
            if 'extra_forbidden' not in feedback or '$defs' not in feedback:
                return response(invalid)
            self.assertNotIn('DO_NOT_ECHO_PRIVATE_INPUT', feedback)
            self.assertNotIn('private original document', feedback)
            return response({'entities': [], 'relations': []})
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result, rejected = await GraphExtractor(GraphSettings(), client).extract({'text': 'private original document'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.model_dump(), {'entities': [], 'relations': []})
        self.assertEqual(rejected, 0)

    async def test_second_invalid_response_still_raises_and_never_sanitizes_metadata(self):
        create = AsyncMock(return_value=response({'entities': [], 'relations': [], '$defs': {}}))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(ValidationError):
            await GraphExtractor(GraphSettings(), client).extract({'text': ''})
        self.assertEqual(create.await_count, 2)

    async def test_feedback_does_not_synthesize_relations_from_validation_errors(self):
        invalid = {'entities': [{'local_id': 'n', 'name': 'owner', 'type': 'NotAType'}], 'relations': []}
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return response(invalid if len(calls) == 1 else {'entities': [], 'relations': []})
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result, _ = await GraphExtractor(GraphSettings(), client).extract({'text': 'owner'})
        self.assertEqual(result.relations, [])
        feedback = calls[-1]['messages'][-1]['content']
        self.assertIn('literal_error', feedback)
        self.assertNotIn('NotAType', feedback)

    async def test_cancelled_request_is_not_retried(self):
        create = AsyncMock(side_effect=asyncio.CancelledError())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(asyncio.CancelledError):
            await GraphExtractor(GraphSettings(), client).extract({'text': ''})
        self.assertEqual(create.await_count, 1)


if __name__ == '__main__':
    unittest.main()
