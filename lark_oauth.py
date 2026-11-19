"""OAuth for Lark (Feishu) user access tokens: login, token storage, auto-refresh.

Configuration (environment variables, or a `.env` file in the working directory):
    LARK_APP_ID         required, the app's App ID (cli_xxx)
    LARK_APP_SECRET     required, the app's App Secret
    LARK_DOMAIN         optional, default https://open.larksuite.com
                        (use https://open.feishu.cn for Feishu China)
    LARK_REDIRECT_PORT  optional, default 9527. Browser login uses
                        http://127.0.0.1:<port>/callback, which must be added to
                        the app's "Redirect URLs" in the Lark developer console.
    LARK_TOKEN_FILE     optional, default ~/.lark-doc-client/tokens.json

CLI:
    python lark_oauth.py login            # browser login (authorization code)
    python lark_oauth.py login --device   # device flow: no redirect URL needed
    python lark_oauth.py status
    python lark_oauth.py logout

Notes:
    * A refresh token is only issued when the `offline_access` scope is requested.
    * Refresh tokens are single use. The new pair is written to disk immediately
      after every refresh. Do not run several processes that refresh at once.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

DEFAULT_DOMAIN = "https://open.larksuite.com"
DEFAULT_SCOPES = ["docx:document:readonly", "wiki:node:read", "bitable:app", "offline_access"]
DEFAULT_REDIRECT_PORT = 9527
REFRESH_SLACK_SECONDS = 300
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class LarkAuthError(RuntimeError):
    """Login, refresh, or token-storage failure."""


# -- configuration -----------------------------------------------------------


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing variables."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def accounts_domain(open_domain: str) -> str:
    """https://open.larksuite.com -> https://accounts.larksuite.com (host used for the consent page)."""
    parts = urllib.parse.urlsplit(open_domain)
    host = parts.netloc
    host = "accounts." + host.removeprefix("open.")
    return urllib.parse.urlunsplit((parts.scheme, host, "", "", ""))


def default_token_file() -> Path:
    return Path(os.environ.get("LARK_TOKEN_FILE") or Path.home() / ".lark-doc-client" / "tokens.json")


# -- token storage -----------------------------------------------------------


class TokenStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path else default_token_file()

    def load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            raise LarkAuthError(f"Cannot read token file {self.path}: {exc}") from exc

    def save(self, tokens: dict[str, Any]) -> None:
        """Atomic write, so a crash cannot leave a half-written (and thus lost) refresh token."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(tokens, handle, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


# -- OAuth client ------------------------------------------------------------


def _post_json(url: str, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """POST JSON and return the parsed body for both 2xx and 4xx (OAuth errors arrive as 400)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise LarkAuthError(f"Network error: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise LarkAuthError(f"Unexpected non-JSON response: {raw[:200]!r}") from None
    if not isinstance(data, dict):
        raise LarkAuthError(f"Unexpected response: {raw[:200]!r}")
    return data


def _error_text(resp: dict[str, Any]) -> str:
    return str(resp.get("error_description") or resp.get("msg") or resp.get("error") or resp)


def _payload(resp: dict[str, Any]) -> dict[str, Any]:
    """Responses are flat in v2, but accept a {"code":0,"data":{...}} envelope too."""
    data = resp.get("data")
    return data if isinstance(data, dict) and "access_token" in data else resp


class LarkOAuth:
    def __init__(self, app_id: str, app_secret: str, domain: str = DEFAULT_DOMAIN):
        if not app_id or not app_secret:
            raise LarkAuthError("LARK_APP_ID and LARK_APP_SECRET are required.")
        self.app_id = app_id
        self._secret = app_secret
        self.domain = domain.rstrip("/")

    @classmethod
    def from_env(cls) -> "LarkOAuth":
        load_dotenv()
        return cls(
            os.environ.get("LARK_APP_ID", ""),
            os.environ.get("LARK_APP_SECRET", ""),
            os.environ.get("LARK_DOMAIN", DEFAULT_DOMAIN),
        )

    def _token_request(self, body: dict[str, Any], what: str) -> dict[str, Any]:
        resp = _post_json(f"{self.domain}/open-apis/authen/v2/oauth/token", {
            "client_id": self.app_id, "client_secret": self._secret, **body,
        })
        return self._to_tokens(resp, what)

    def _to_tokens(self, resp: dict[str, Any], what: str) -> dict[str, Any]:
        data = _payload(resp)
        if not data.get("access_token"):
            raise LarkAuthError(f"{what} failed: {_error_text(resp)}")
        now = time.time()
        expires_in = int(data.get("expires_in") or 0)
        refresh_expires_in = int(data.get("refresh_token_expires_in") or 0)
        return {
            "app_id": self.app_id,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": now + expires_in if expires_in else None,
            "refresh_expires_at": now + refresh_expires_in if refresh_expires_in else None,
            "scope": data.get("scope", ""),
        }

    # authorization code flow
    def authorize_url(self, redirect_uri: str, scopes: list[str], state: str) -> str:
        query = urllib.parse.urlencode({
            "client_id": self.app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        })
        return f"{accounts_domain(self.domain)}/open-apis/authen/v1/authorize?{query}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            "Code exchange",
        )

    # device flow
    def device_start(self, scopes: list[str]) -> dict[str, Any]:
        resp = _post_json(f"{self.domain}/open-apis/authen/v2/oauth/device_authorization", {
            "client_id": self.app_id, "scope": " ".join(scopes),
        })
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        if not data.get("device_code"):
            raise LarkAuthError(f"Device authorization failed: {_error_text(resp)}")
        return data

    def device_poll(self, init: dict[str, Any]) -> dict[str, Any]:
        interval = max(1, int(init.get("interval") or 5))
        deadline = time.time() + int(init.get("expires_in") or 600)
        while time.time() < deadline:
            resp = _post_json(f"{self.domain}/open-apis/authen/v2/oauth/token", {
                "client_id": self.app_id,
                "client_secret": self._secret,
                "grant_type": DEVICE_GRANT,
                "device_code": init["device_code"],
            })
            if _payload(resp).get("access_token"):
                return self._to_tokens(resp, "Device login")
            error = resp.get("error")
            if error == "authorization_pending":
                pass
            elif error == "slow_down":
                interval += 2
            else:
                raise LarkAuthError(f"Device login failed: {_error_text(resp)}")
            time.sleep(interval)
        raise LarkAuthError("Device login timed out.")

    # refresh
    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}, "Token refresh"
        )


# -- login flows -------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).result = params
        ok = "code" in params and "error" not in params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = "Login complete. You can close this tab." if ok else "Login failed. Return to the terminal."
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode("utf-8"))

    def log_message(self, *args: Any) -> None:  # silence request logging
        pass


def login_browser(oauth: LarkOAuth, scopes: list[str], port: int, timeout: float = 300.0) -> dict[str, Any]:
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(24)
    _CallbackHandler.result = None
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise LarkAuthError(f"Cannot listen on 127.0.0.1:{port}: {exc}") from exc
    server.timeout = 1.0

    url = oauth.authorize_url(redirect_uri, scopes, state)
    print(f"Redirect URL that must be registered in the Lark console: {redirect_uri}")
    print(f"Opening browser. If nothing opens, visit:\n{url}\n")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    deadline = time.time() + timeout
    try:
        while _CallbackHandler.result is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    params = _CallbackHandler.result
    if params is None:
        raise LarkAuthError("Timed out waiting for the browser login.")
    if not secrets.compare_digest(params.get("state", ""), state):
        raise LarkAuthError("State mismatch in OAuth callback; login aborted.")
    if "error" in params or "code" not in params:
        raise LarkAuthError(
            f"Authorization denied: {params.get('error_description') or params.get('error') or 'no code returned'}"
        )
    return oauth.exchange_code(params["code"], redirect_uri)


def login_device(oauth: LarkOAuth, scopes: list[str]) -> dict[str, Any]:
    init = oauth.device_start(scopes)
    url = init.get("verification_uri_complete") or init.get("verification_uri")
    print(f"Open this URL and approve access:\n  {url}")
    if init.get("user_code"):
        print(f"Code: {init['user_code']}")
    print("Waiting for approval...")
    return oauth.device_poll(init)


# -- token provider used by the doc client -----------------------------------


class UserTokenProvider:
    """Returns a valid user_access_token, refreshing and persisting it when needed."""

    def __init__(self, oauth: LarkOAuth, store: TokenStore | None = None):
        self._oauth = oauth
        self._store = store or TokenStore()

    def get(self, force_refresh: bool = False) -> str:
        tokens = self._store.load()
        if not tokens or not tokens.get("access_token"):
            raise LarkAuthError("Not logged in. Run: python lark_oauth.py login")
        if tokens.get("app_id") and tokens["app_id"] != self._oauth.app_id:
            raise LarkAuthError(
                f"Stored tokens belong to app {tokens['app_id']}, not {self._oauth.app_id}. "
                "Run: python lark_oauth.py login"
            )
        expires_at = tokens.get("expires_at")
        stale = expires_at is not None and expires_at - time.time() <= REFRESH_SLACK_SECONDS
        if not (force_refresh or stale):
            return tokens["access_token"]

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise LarkAuthError(
                "Access token expired and no refresh token is stored "
                "(was `offline_access` requested?). Run: python lark_oauth.py login"
            )
        refresh_expires_at = tokens.get("refresh_expires_at")
        if refresh_expires_at is not None and refresh_expires_at <= time.time():
            raise LarkAuthError("Refresh token expired. Run: python lark_oauth.py login")
        new_tokens = self._oauth.refresh(refresh_token)
        # Keep the old refresh token only if the server did not rotate it.
        if not new_tokens.get("refresh_token"):
            new_tokens["refresh_token"] = refresh_token
            new_tokens["refresh_expires_at"] = tokens.get("refresh_expires_at")
        self._store.save(new_tokens)
        return new_tokens["access_token"]


# -- CLI ---------------------------------------------------------------------


def _fmt_time(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    delta = int(ts - time.time())
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return f"{when} ({'in ' + str(delta) + 's' if delta > 0 else 'expired'})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lark OAuth login for the doc client.")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="log in and store tokens")
    login.add_argument("--device", action="store_true", help="use the device flow (no redirect URL)")
    login.add_argument("--scope", nargs="+", default=DEFAULT_SCOPES, help="scopes to request")
    login.add_argument("--port", type=int,
                       default=int(os.environ.get("LARK_REDIRECT_PORT", DEFAULT_REDIRECT_PORT)))
    sub.add_parser("status", help="show stored token state")
    sub.add_parser("logout", help="delete stored tokens")
    args = parser.parse_args(argv)

    load_dotenv()
    store = TokenStore()
    try:
        if args.command == "login":
            oauth = LarkOAuth.from_env()
            tokens = login_device(oauth, args.scope) if args.device else login_browser(oauth, args.scope, args.port)
            store.save(tokens)
            print(f"Logged in. Tokens saved to {store.path}")
            if not tokens.get("refresh_token"):
                print("Warning: no refresh token returned. Add `offline_access` to the scopes "
                      "and to the app's permissions.", file=sys.stderr)
        elif args.command == "status":
            tokens = store.load()
            if not tokens:
                print("Not logged in.")
                return 1
            print(f"File:            {store.path}")
            print(f"App ID:          {tokens.get('app_id')}")
            print(f"Scopes:          {tokens.get('scope') or 'unknown'}")
            print(f"Access expires:  {_fmt_time(tokens.get('expires_at'))}")
            print(f"Refresh token:   {'yes' if tokens.get('refresh_token') else 'no'}"
                  f", expires {_fmt_time(tokens.get('refresh_expires_at'))}")
        else:
            print("Tokens deleted." if store.clear() else "No stored tokens.")
    except LarkAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
