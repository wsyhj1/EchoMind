"""Frontend listener policy must apply before cached local identity is returned."""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import streamlit as st
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_client import _origin


def response(payload):
    result = MagicMock(status_code=200)
    result.json.return_value = payload
    return result


class IdentityListenerUITests(unittest.TestCase):
    def app(self, cached_mode=None):
        app = AppTest.from_string('import streamlit as st\nfrom identity_ui import bootstrap_identity\n'
                                  'st.session_state.guarded_user_id=bootstrap_identity("http://localhost:8000")', default_timeout=15)
        app.session_state.identity_api_base = 'http://localhost:8000'
        if cached_mode:
            app.session_state.identity_info = {'user_id': 7, 'mode': cached_mode, 'username': 'test'}
            app.session_state.identity_checked_at = time.monotonic()
        return app

    def listener(self, value):
        original = st.get_option
        return patch.object(st, 'get_option', side_effect=lambda key: value if key == 'server.address' else original(key))

    def test_cached_local_identity_cannot_bypass_public_or_unspecified_listener_guard(self):
        for address in ('0.0.0.0', '::', None, 'localhost.example', '127.0.0.2'):
            with self.subTest(address=address), self.listener(address), patch('requests.get') as get:
                app = self.app('local').run()
                self.assertFalse(app.exception)
                self.assertTrue(any('本机模式只允许' in item.value for item in app.error))
                self.assertNotIn('guarded_user_id', app.session_state)
                get.assert_not_called()

    def test_fresh_local_mode_rejects_public_listener_before_me_request(self):
        with self.listener('0.0.0.0'), patch('requests.get', return_value=response({'mode': 'local'})) as get:
            app = self.app().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertEqual(get.call_count, 1)
            self.assertTrue(get.call_args.args[0].endswith('/auth/status'))
            self.assertNotIn('guarded_user_id', app.session_state)

    def test_each_explicit_loopback_listener_keeps_cached_local_identity_usable(self):
        for address in ('127.0.0.1', 'localhost', '::1'):
            with self.subTest(address=address), self.listener(address), patch('requests.get') as get:
                app = self.app('local').run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertEqual(app.session_state.guarded_user_id, 7)
                get.assert_not_called()

    def test_password_mode_can_authenticate_on_public_listener(self):
        with self.listener('0.0.0.0'), patch('requests.get', side_effect=[
                response({'mode': 'password'}), response({'user_id': 7, 'mode': 'password', 'username': 'test'})]) as get:
            app = self.app()
            app.session_state.identity_token = 'test-not-real-token'
            app.session_state.identity_token_origin = _origin('http://localhost:8000')
            app.run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertEqual(app.session_state.guarded_user_id, 7)
            self.assertEqual(get.call_count, 2)

    def test_server_mode_change_during_bootstrap_is_checked_again_before_return(self):
        with self.listener('0.0.0.0'), patch('requests.get', side_effect=[
                response({'mode': 'password'}), response({'user_id': 7, 'mode': 'local', 'username': 'test'})]):
            app = self.app()
            app.session_state.identity_token = 'test-not-real-token'
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertNotIn('guarded_user_id', app.session_state)


if __name__ == '__main__':
    unittest.main()
