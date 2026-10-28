"""Independent transport checks: mocked sessions and HTTP, no accounts/network."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_client
import identity_ui


class IdentityTransportReviewTests(unittest.TestCase):
    def setUp(self):
        self.state = {"identity_api_base": "http://localhost:8000", "identity_token": "review-token-not-real"}
        self.state["identity_token_origin"] = api_client._origin(self.state["identity_api_base"])
        self.state_patch = patch.object(api_client.st, "session_state", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_all_used_http_verbs_including_profile_patch_have_session_auth(self):
        for method in ("get", "post", "put", "patch", "delete"):
            with self.subTest(method=method), patch.object(api_client._requests, method) as transport:
                getattr(api_client.api_requests, method)("http://localhost:8000/user-profile/settings")
                self.assertEqual(transport.call_args.kwargs.get("headers", {}).get("Authorization"), "Bearer review-token-not-real")
                self.assertFalse(transport.call_args.kwargs.get("allow_redirects", True))

    def test_cross_origin_never_receives_session_or_explicit_captured_bearer(self):
        with patch.object(api_client._requests, "get") as transport:
            api_client.api_requests.get("https://unrelated.invalid/source", headers={"Authorization": "Bearer review-token-not-real", "Accept": "application/json"})
            headers = transport.call_args.kwargs.get("headers", {})
            self.assertFalse(any(key.casefold() == "authorization" for key in headers))
            self.assertEqual(headers.get("Accept"), "application/json")

    def test_callers_cannot_override_the_no_redirect_policy(self):
        with patch.object(api_client._requests, "get") as transport:
            api_client.api_requests.get("http://localhost:8000/runs", allow_redirects=True)
            self.assertIs(transport.call_args.kwargs.get("allow_redirects"), False)

    def test_current_session_identity_is_read_per_call_without_global_reuse(self):
        with patch.object(api_client._requests, "get") as transport:
            api_client.api_requests.get("http://localhost:8000/runs")
            self.assertEqual(transport.call_args.kwargs["headers"]["Authorization"], "Bearer review-token-not-real")
            self.state["identity_token"] = "second-session-token-not-real"
            api_client.api_requests.get("http://localhost:8000/runs")
            self.assertEqual(transport.call_args.kwargs["headers"]["Authorization"], "Bearer second-session-token-not-real")
            self.state.pop("identity_token")
            api_client.api_requests.get("http://localhost:8000/runs")
            self.assertNotIn("Authorization", transport.call_args.kwargs.get("headers", {}))

    def test_unbound_token_is_not_sent(self):
        self.state.pop("identity_token_origin")
        with patch.object(api_client._requests, "get") as transport:
            api_client.api_requests.get("http://localhost:8000/runs")
            self.assertNotIn("Authorization", transport.call_args.kwargs.get("headers", {}))

    def test_old_issuer_token_is_not_sent_after_base_changes(self):
        self.state["identity_api_base"] = "https://second-server.invalid"
        with patch.object(api_client._requests, "get") as transport:
            api_client.api_requests.get("https://second-server.invalid/runs")
            self.assertNotIn("Authorization", transport.call_args.kwargs.get("headers", {}))

    def test_bootstrap_base_change_discards_identity_and_cached_workspace_before_any_request(self):
        class State(dict):
            __getattr__ = dict.__getitem__
            __setattr__ = dict.__setitem__
        class StopProbe(RuntimeError):
            pass
        state = State(self.state)
        state.update(identity_info={"user_id": 1}, identity_checked_at=10**12,
                     profile_data={"private": "old account"}, run_detail_cache={"old": "snapshot"})
        params = SimpleNamespace(clear=Mock())
        with patch.object(identity_ui.st, "session_state", state), patch.object(identity_ui.st, "query_params", params), \
             patch.object(identity_ui.requests, "get", side_effect=StopProbe("before network")):
            with self.assertRaises(StopProbe):
                identity_ui.bootstrap_identity("https://second-server.invalid")
        self.assertNotIn("identity_token", state)
        self.assertNotIn("identity_token_origin", state)
        self.assertNotIn("identity_info", state)
        self.assertNotIn("profile_data", state)
        self.assertNotIn("run_detail_cache", state)
        params.clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
