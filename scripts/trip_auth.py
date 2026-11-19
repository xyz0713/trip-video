"""Personal Colab OAuth for trip, using google-colab-cli==0.8.0 on Python 3.12+.

Install: python -m pip install 'google-colab-cli==0.8.0'
Windows' pywin32-ctypes backend is installed automatically; the upstream `colab` executable itself
does not support Windows. This adapter uses its auth resources and Client only.
Linux requires an available, unlocked desktop Secret Service and user D-Bus.

Run `python trip_auth.py login --account owner@example.test --project trip-demo-project`
with your actual account/project once, in the same persistent Python environment
that will run trip. Future processes call session() without opening OAuth.
No ADC, service account, upstream CLI token, or plaintext fallback is used.
An OS vault access/unlock prompt can still require the owner; grant access at
login. Keep the environment path stable (especially for macOS Keychain ACLs).
"""

from __future__ import annotations

import hashlib
import argparse
from importlib import metadata, resources
import json
import logging
import re
from pathlib import Path
import subprocess
import sys

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


SERVICE = "trip-video-colab-oauth-v1"
SLOT = "current-user"
USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"
TIMEOUT = 30
# Colab's runtime backend needs colaboratory; userinfo.email verifies the owner.
# Drive access and the CLI's broader cloud-platform/profile scopes are unused.
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
          "https://www.googleapis.com/auth/colaboratory"]


class AuthError(RuntimeError):
    """An actionable error whose message never contains OAuth secrets."""


def _settings(account: str, project: str) -> tuple[str, str]:
    domain = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    if (not isinstance(account, str) or len(account) > 254
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@" + domain + r"(?:\." + domain + r")+", account)
            or account.split("@")[0].endswith(".") or ".." in account):
        raise AuthError("Provide a valid Google account email with --account.")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise AuthError("Provide a Google Cloud project ID (6-30 lowercase letters, digits or hyphens) with --project.")
    return account.lower(), project


def _silence_oauth_logs() -> None:
    # The callback handler logs the authorization code at INFO, and requests-
    # oauthlib logs token bodies at DEBUG. Keep these exact dependency loggers
    # disabled even if the caller enables verbose application logging.
    for name in ("google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session"):
        logging.getLogger(name).disabled = True


def _config() -> dict:
    if metadata.version("google-colab-cli") != "0.8.0":
        raise AuthError("Install google-colab-cli==0.8.0 in trip's Python environment.")
    return json.loads(resources.files("colab_cli").joinpath("oauth_config.json").read_text())


def _storage():
    """Choose a known encrypted OS vault, ignoring arbitrary keyring plugins."""
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            backend = Keyring()
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            backend = WinVaultKeyring()
            # Durable on this machine, never Windows' session-only persistence.
            backend.persist = "local machine"
        elif sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
            backend = Keyring()
        else:
            raise AuthError("trip requires macOS Keychain, Windows Credential Manager, or Linux Secret Service.")
        if backend.priority <= 0:
            raise RuntimeError("unavailable")
        return backend
    except AuthError:
        raise
    except Exception:
        raise AuthError("Secure OS credential storage is unavailable. Enable/unlock your OS vault; no token file will be created.") from None


def _read(backend) -> dict:
    try:
        raw = backend.get_password(SERVICE, SLOT)
        if raw is None:
            raise AuthError("No trip login on this PC. Run trip login first.")
        data = json.loads(raw)
        if (data.get("version") != 2
                or not isinstance(data.get("refresh_token"), str)
                or not data["refresh_token"]):
            raise AuthError("Saved trip login is invalid. Run trip login again.")
        account, project = _settings(data.get("email"), data.get("project"))
        data["email"], data["project"] = account, project
        return data
    except AuthError:
        raise
    except Exception:
        raise AuthError("Cannot read trip's OS vault entry. Unlock the vault or run trip login again.") from None


def _encoded(credentials, account: str, project: str) -> str:
    account, project = _settings(account, project)
    if not credentials.refresh_token:
        raise AuthError("Google did not grant offline access. Run trip login again and approve access.")
    raw = json.dumps({"version": 2, "email": account, "project": project,
                      "refresh_token": credentials.refresh_token}, separators=(",", ":"))
    # WinVault generic credentials have a 2560-byte limit. Store no access/id
    # token or client config, and fail before writing if even this is too large.
    if len(raw.encode("utf-16-le")) > 2500:
        raise AuthError("Google's refresh credential exceeds the secure vault limit.")
    return raw


def _save(backend, credentials, account: str, project: str) -> str:
    raw = _encoded(credentials, account, project)
    try:
        backend.set_password(SERVICE, SLOT, raw)
        if backend.get_password(SERVICE, SLOT) != raw:
            raise RuntimeError("vault readback mismatch")
    except Exception:
        raise AuthError("The OS vault did not save trip login. Unlock/authorize the vault and retry trip login.") from None
    return raw


def _probe_storage(raw: str) -> None:
    """Require a fresh interpreter to see the saved credential before success."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "_check_storage"],
            input=hashlib.sha256(raw.encode()).hexdigest(), text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TIMEOUT,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("vault entry not visible across processes")
    except Exception:
        raise AuthError("Saved trip login is not readable by a fresh Python process. Use a persistent Python installation outside the sandbox and authorize its OS vault access.") from None


def _verified(credentials, account: str, project: str) -> AuthorizedSession:
    client = AuthorizedSession(credentials)
    try:
        response = client.get(USERINFO, timeout=TIMEOUT)
        response.raise_for_status()
        identity = response.json()
        if identity.get("email") != account or identity.get("verified_email") is not True:
            raise AuthError(f"Google account must be exactly {account}. Run trip login and select that account.")
        client.trip_owner = account
        client.credentials = credentials.with_quota_project(project)
        client.headers["x-goog-user-project"] = project
        return client
    except AuthError:
        client.close()
        raise
    except Exception:
        client.close()
        raise AuthError("Could not verify the Google account. Check network access or run trip login again.") from None


def login(account: str, project: str) -> AuthorizedSession:
    """Interactively authorize the owner and save an OS-vault refresh credential."""
    _silence_oauth_logs()
    account, project = _settings(account, project)
    config = _config()
    backend = _storage()
    try:
        flow = InstalledAppFlow.from_client_config(
            config, SCOPES, autogenerate_code_verifier=True,
        )
        credentials = flow.run_local_server(
            host="localhost", port=0, open_browser=True, timeout_seconds=300,
            login_hint=account, prompt="consent select_account", access_type="offline",
            authorization_prompt_message="Open this URL to sign in to trip:\n{url}",
            success_message="Google authorization received. Return to trip to check the account and saved login.",
        )
    except Exception:
        raise AuthError("Google login did not complete. Run trip login again on this PC.") from None
    client = _verified(credentials, account, project)
    try:
        raw = _save(backend, credentials, account, project)
        _probe_storage(raw)
    except Exception:
        client.close()
        raise
    return client


def session() -> AuthorizedSession:
    """Load, refresh and verify this PC's owner credential; never starts OAuth."""
    _silence_oauth_logs()
    config = _config()["installed"]
    backend = _storage()
    saved = _read(backend)
    credentials = Credentials(
        token=None, refresh_token=saved["refresh_token"],
        token_uri=config["token_uri"], client_id=config["client_id"],
        client_secret=config["client_secret"], scopes=SCOPES,
    )
    try:
        request = Request()
        credentials.refresh(lambda *args, **kwargs: request(*args, **{**kwargs, "timeout": TIMEOUT}))
    except Exception:
        raise AuthError("Saved Google login could not refresh. Check network access or run trip login again.") from None
    client = _verified(credentials, saved["email"], saved["project"])
    try:
        if credentials.refresh_token != saved["refresh_token"]:
            _save(backend, credentials, saved["email"], saved["project"])
    except Exception:
        client.close()
        raise
    return client


def _main() -> int:
    try:
        if sys.argv[1:] == ["_check_storage"]:
            raw = json.dumps(_read(_storage()), separators=(",", ":"))
            return 0 if hashlib.sha256(raw.encode()).hexdigest() == sys.stdin.read().strip() else 1
        parser = argparse.ArgumentParser(description="Authorize trip with your Google account and quota project.")
        commands = parser.add_subparsers(dest="command", required=True)
        authorize = commands.add_parser("login")
        authorize.add_argument("--account", required=True)
        authorize.add_argument("--project", required=True)
        commands.add_parser("check")
        args = parser.parse_args()
        if args.command == "login":
            client = login(args.account, args.project)
        else:
            client = session()
        client.close()
        print(f"Google account verified; quota project {client.credentials.quota_project_id}.")
        return 0
    except AuthError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("trip authentication failed. Check the Python dependencies and OS vault, then run trip login.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
