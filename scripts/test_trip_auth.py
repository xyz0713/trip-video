"""Offline tests. Run with the trip Python: python -m unittest test_trip_auth -v."""

from contextlib import ExitStack
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

import trip_auth as auth


ACCOUNT = "owner@example.test"
PROJECT = "trip-demo-project"


CONFIG = {"installed": {"client_id": "official-native-client",
                         "client_secret": "public-native-client-secret",
                         "token_uri": "https://oauth2.googleapis.com/token"}}


class FakeStorage:
    def __init__(self):
        self.value = None

    def get_password(self, service, account):
        assert (service, account) == (auth.SERVICE, auth.SLOT)
        return self.value

    def set_password(self, service, account, value):
        assert (service, account) == (auth.SERVICE, auth.SLOT)
        self.value = value


class FakeCredentials:
    def __init__(self, **kwargs):
        self.refresh_token = kwargs.get("refresh_token", "fake-refresh-secret")
        self.token = "fake-access-secret"
        self.quota_project_id = None
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True

    def with_quota_project(self, project):
        self.quota_project_id = project
        return self


class FakeSession:
    identity = {"email": ACCOUNT, "verified_email": True}

    def __init__(self, credentials):
        self.credentials = credentials
        self.headers = {}
        self.closed = False

    def get(self, url, **kwargs):
        assert url == auth.USERINFO
        assert kwargs["timeout"] == auth.TIMEOUT
        return Mock(json=lambda: self.identity, raise_for_status=lambda: None)

    def close(self):
        self.closed = True


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = FakeStorage()
        for name, value in [("_config", lambda: CONFIG),
                            ("_storage", lambda: self.store),
                            ("Credentials", FakeCredentials),
                            ("AuthorizedSession", FakeSession)]:
            self.stack.enter_context(patch.object(auth, name, value))

    def seed(self):
        self.store.value = auth._encoded(FakeCredentials(), ACCOUNT, PROJECT)

    def test_session_refreshes_verifies_and_pins_project_without_oauth(self):
        self.seed()
        with patch.object(auth.InstalledAppFlow, "from_client_config") as interactive:
            client = auth.session()
        self.assertTrue(client.credentials.refreshed)
        self.assertEqual(client.credentials.quota_project_id, PROJECT)
        self.assertEqual(client.headers["x-goog-user-project"], PROJECT)
        interactive.assert_not_called()

    def test_missing_token_fails_without_oauth(self):
        with patch.object(auth.InstalledAppFlow, "from_client_config") as interactive:
            with self.assertRaisesRegex(auth.AuthError, "trip login"):
                auth.session()
        interactive.assert_not_called()

    def test_login_requires_valid_explicit_configuration_before_oauth(self):
        invalid = [("bad-email", PROJECT), ("owner@example..test", PROJECT),
                   ("owner.@example.test", PROJECT), (" owner@example.test", PROJECT),
                   (ACCOUNT, "123-project"), (ACCOUNT, "Uppercase-project"),
                   (ACCOUNT, "short"), (ACCOUNT, "ends-with-hyphen-"),
                   (None, PROJECT), (ACCOUNT, None)]
        with patch.object(auth.InstalledAppFlow, "from_client_config") as interactive:
            for account, project in invalid:
                with self.subTest(account=account, project=project), self.assertRaises(auth.AuthError):
                    auth.login(account, project)
        interactive.assert_not_called()
        self.assertIsNone(self.store.value)

    def test_session_uses_saved_account_and_project(self):
        alternate = "another@example.test"
        project = "another-demo-project"
        self.store.value = auth._encoded(FakeCredentials(), alternate, project)
        with patch.object(FakeSession, "identity", {"email": alternate, "verified_email": True}):
            client = auth.session()
        self.assertEqual(client.credentials.quota_project_id, project)
        self.assertEqual(client.headers["x-goog-user-project"], project)

    def test_invalid_saved_configuration_fails_closed(self):
        for changes in [{"project": "INVALID"}, {"email": "invalid"}, {"project": None}]:
            self.seed()
            data = json.loads(self.store.value)
            data.update(changes)
            self.store.value = json.dumps(data)
            with self.subTest(changes=changes), self.assertRaises(auth.AuthError):
                auth.session()

    def test_invalid_storage_fails_without_oauth(self):
        for raw in ["invalid-json", "{}", json.dumps({"version": 1, "email": "other@example.test", "refresh_token": "secret"})]:
            with self.subTest(raw=raw):
                self.store.value = raw
                with self.assertRaises(auth.AuthError):
                    auth.session()

    def test_wrong_or_unverified_identity_is_rejected(self):
        self.seed()
        for identity in [{"email": "other@example.test", "verified_email": True},
                         {"email": ACCOUNT, "verified_email": False},
                         {"email": ACCOUNT, "verified_email": "true"}, {}]:
            with self.subTest(identity=identity), patch.object(FakeSession, "identity", identity):
                with self.assertRaisesRegex(auth.AuthError, "exactly"):
                    auth.session()

    def test_refresh_failure_is_sanitized_and_no_oauth(self):
        self.seed()
        with patch.object(FakeCredentials, "refresh", side_effect=ValueError("fake-refresh-secret")):
            with self.assertRaises(auth.AuthError) as error:
                auth.session()
        self.assertNotIn("fake-refresh-secret", str(error.exception))

    def test_login_verifies_before_saving_and_stores_refresh_only(self):
        flow = Mock(run_local_server=Mock(return_value=FakeCredentials()))
        with patch.object(auth.InstalledAppFlow, "from_client_config", return_value=flow) as create_flow, patch.object(auth, "_probe_storage") as probe:
            client = auth.login(ACCOUNT, PROJECT)
        create_flow.assert_called_once_with(CONFIG, auth.SCOPES, autogenerate_code_verifier=True)
        self.assertEqual(client.headers["x-goog-user-project"], PROJECT)
        args = flow.run_local_server.call_args.kwargs
        self.assertEqual(args["login_hint"], ACCOUNT)
        self.assertEqual(args["port"], 0)
        self.assertIn("select_account", args["prompt"])
        probe.assert_called_once_with(self.store.value)
        self.assertEqual(set(json.loads(self.store.value)), {"version", "email", "project", "refresh_token"})
        self.assertNotIn("fake-access-secret", self.store.value)
        self.assertNotIn("client_secret", self.store.value)

    def test_wrong_login_never_overwrites_good_credential(self):
        self.seed()
        saved = self.store.value
        with patch.object(auth.InstalledAppFlow, "from_client_config", return_value=Mock(run_local_server=lambda **kwargs: FakeCredentials())), patch.object(FakeSession, "identity", {"email": "other@example.test", "verified_email": True}):
            with self.assertRaises(auth.AuthError):
                auth.login(ACCOUNT, PROJECT)
        self.assertEqual(self.store.value, saved)

    def test_no_plaintext_fallback_after_storage_failure(self):
        self.seed()
        with patch.object(self.store, "get_password", side_effect=RuntimeError("fake-refresh-secret")):
            with self.assertRaises(auth.AuthError) as error:
                auth.session()
        self.assertNotIn("fake-refresh-secret", str(error.exception))

    def test_storage_readback_and_size_limit_fail_closed(self):
        with patch.object(self.store, "get_password", return_value=None):
            with self.assertRaises(auth.AuthError):
                auth._save(self.store, FakeCredentials(), ACCOUNT, PROJECT)
        with self.assertRaises(auth.AuthError):
            auth._encoded(FakeCredentials(refresh_token="x" * 2000), ACCOUNT, PROJECT)

    def test_missing_offline_token_is_rejected(self):
        with self.assertRaisesRegex(auth.AuthError, "offline access"):
            auth._encoded(FakeCredentials(refresh_token=None), ACCOUNT, PROJECT)

    def test_probe_uses_same_python_without_secret_in_arguments_or_output(self):
        self.seed()
        with patch.object(auth.subprocess, "run", return_value=Mock(returncode=0)) as run:
            auth._probe_storage(self.store.value)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertNotIn("fake-refresh-secret", str(args))
        self.assertEqual(kwargs["input"], hashlib.sha256(self.store.value.encode()).hexdigest())
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        with patch.object(auth.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(auth.AuthError, "fresh Python process"):
                auth._probe_storage(self.store.value)

    def test_dependency_callback_and_token_logging_are_suppressed(self):
        names = ("google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session")
        previous = [logging.getLogger(name).disabled for name in names]
        try:
            for name in names:
                logging.getLogger(name).disabled = False
            auth._silence_oauth_logs()
            for name in names:
                self.assertTrue(logging.getLogger(name).disabled)
        finally:
            for name, disabled in zip(names, previous):
                logging.getLogger(name).disabled = disabled

    def test_fake_vault_credentials_reused_by_fresh_process(self):
        # Only this test fake writes JSON. Production selects native encrypted
        # vault classes directly and has no environment-selected storage path.
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "process.py"
            vault = Path(directory) / "fake-vault.json"
            wrapper.write_text(textwrap.dedent('''
                import json, pathlib, sys
                sys.path.insert(0, sys.argv[1])
                import trip_auth as a
                from test_trip_auth import ACCOUNT, PROJECT, CONFIG, FakeCredentials, FakeSession
                path = pathlib.Path(sys.argv[2])
                class FakeVault:
                    def get_password(self, *args):
                        return path.read_text() if path.exists() else None
                    def set_password(self, service, account, value):
                        path.write_text(value)
                a._config = lambda: CONFIG
                a._storage = FakeVault
                a.Credentials = FakeCredentials
                a.AuthorizedSession = FakeSession
                a._probe_storage = lambda raw: None
                class Flow:
                    def run_local_server(self, **kwargs): return FakeCredentials()
                a.InstalledAppFlow.from_client_config = lambda *args, **kwargs: Flow()
                if sys.argv[3] == 'login':
                    a.login(ACCOUNT, PROJECT).close()
                else:
                    # Fail the test if any interactive OAuth is attempted.
                    a.InstalledAppFlow.from_client_config = lambda *args: (_ for _ in ()).throw(AssertionError('interactive'))
                    client = a.session()
                    assert client.credentials.refreshed
                    assert client.headers['x-goog-user-project'] == PROJECT
                    client.close()
                print('verified')
            '''))
            for command in ["login", "session", "session"]:
                result = subprocess.run([sys.executable, str(wrapper), str(Path(auth.__file__).parent), str(vault), command], text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "verified\n")
                self.assertNotIn("fake-refresh-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
