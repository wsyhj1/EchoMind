"""Exercise the actual settings button's API target, without a live network."""
import os
import unittest
from unittest.mock import MagicMock, patch
import test_workspace_ui as fixtures


class ButtonTransportTests(unittest.TestCase):
    setUp = fixtures.WorkspaceUITest.setUp
    app = fixtures.WorkspaceUITest.app
    settings_response = fixtures.WorkspaceUITest.settings_response

    def test_default_settings_click_uses_bound_ipv4_backend_without_localhost_fallback(self):
        with patch.dict(os.environ):
            os.environ.pop('ECHOMIND_API_BASE_URL', None)
            app = self.app({'默认知识库': []}).run(timeout=45)
            result = MagicMock(status_code=200)
            result.json.return_value = self.settings_response()
            with patch('requests.get', return_value=result) as get:
                app.button(key='open_settings').click().run()
            self.assertFalse(app.exception)
            get.assert_called_once()
            self.assertEqual(get.call_args.args[0], 'http://127.0.0.1:8000/user-settings')

    def test_explicit_backend_origin_is_preserved(self):
        with patch.dict(os.environ, {'ECHOMIND_API_BASE_URL': 'https://configured.invalid/api'}):
            app = self.app({'默认知识库': []}).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['identity_api_base'], 'https://configured.invalid/api')
