"""Execute actual app functions without importing startup/UI or mirroring workers."""
import ast
import asyncio
from pathlib import Path
from queue import Empty, Queue
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


class BackgroundSessionIsolationTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'app.py'
        tree = ast.parse(source.read_text('utf-8'))
        wanted = {'init_session_state', '_start_background', 'process_async_results', 'apply_async_load_result'}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
        self.assertEqual({node.name for node in nodes}, wanted)
        self.targets = []
        targets = self.targets
        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                targets.append(self.target)
        self.st = SimpleNamespace(session_state=State(), rerun=Mock(), error=Mock(), toast=Mock(), warning=Mock())
        self.ns = {'st': self.st, 'Queue': Queue, 'Empty': Empty, 'asyncio': asyncio, 'time': time,
                   'Thread': DeferredThread, 'USER_ID': 1,
                   'transport_headers': lambda: {'Authorization': self.st.session_state['token']}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), self.ns)

    def state(self, token):
        self.st.session_state = State(token=token)
        self.ns['init_session_state']()
        return self.st.session_state

    def test_each_session_owns_its_queue_and_rerun_keeps_only_its_existing_queue(self):
        first = self.state('first-token')
        second = self.state('second-token')
        self.assertIsNot(first.task_inbox, second.task_inbox)
        queue = second.task_inbox
        self.ns['init_session_state']()
        self.assertIs(self.st.session_state.task_inbox, queue)

    def test_delayed_completion_keeps_captured_owner_headers_and_original_queue(self):
        calls = []
        async def operation(*, user_id, auth_headers):
            calls.append((user_id, auth_headers))
            return {'status': 'success', 'owner': user_id}
        first = self.state('first-token')
        self.ns['USER_ID'] = 1
        self.ns['_start_background']('load', operation)
        second = self.state('second-token')
        self.ns['USER_ID'] = 2
        self.ns['_start_background']('load', operation)
        # Both workers finish after the currently active UI has become session2.
        self.targets[0]()
        self.targets[1]()
        self.assertEqual(calls, [(1, {'Authorization': 'first-token'}), (2, {'Authorization': 'second-token'})])
        one, two = first.task_inbox.get_nowait(), second.task_inbox.get_nowait()
        self.assertEqual((one['user_id'], one['result']['owner']), (1, 1))
        self.assertEqual((two['user_id'], two['result']['owner']), (2, 2))
        self.assertTrue(first.task_inbox.empty() and second.task_inbox.empty())
        self.assertFalse(any(key.startswith('_') for key in vars(self.st)))

    def test_foreign_owner_result_is_discarded_before_any_state_change_or_rerun(self):
        state = self.state('second-token')
        self.ns['USER_ID'] = 2
        state.task_inbox.put({'kind': 'load', 'user_id': 1, 'metadata': {},
                              'result': {'knowledge_bases': {'private-first-user': []}}})
        self.ns['process_async_results']()
        self.assertEqual(state.knowledge_bases, {})
        self.st.rerun.assert_not_called()
        state.task_inbox.put({'kind': 'load', 'user_id': 2, 'metadata': {},
                              'result': {'knowledge_bases': {'own': []}, 'selected_kb': 'own'}})
        self.ns['process_async_results']()
        self.assertEqual(state.knowledge_bases, {'own': []})
        self.assertEqual(state.selected_kb, 'own')
        self.st.rerun.assert_called_once()

    def test_worker_failure_does_not_write_global_error_or_exception_secrets(self):
        async def failure(**identity):
            raise RuntimeError('Bearer private-token-with-source-content')
        first = self.state('first-token')
        self.ns['_start_background']('upload', failure, {'kb_id': 'kb', 'file_name': 'own.pdf'})
        second = self.state('second-token')
        self.ns['USER_ID'] = 2
        self.targets[0]()
        result = first.task_inbox.get_nowait()
        self.assertEqual(result['user_id'], 1)
        self.assertEqual(result['result']['error'], 'RuntimeError')
        self.assertNotIn('private-token', str(result))
        self.assertTrue(second.task_inbox.empty())
        self.assertFalse(any(key.startswith('_') for key in vars(self.st)))


if __name__ == '__main__':
    unittest.main()
