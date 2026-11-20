"""Lark Base (Bitable) client that authenticates as the app (tenant access token).

Configuration (environment variables, or a `.env` file in the working directory):
    LARK_APP_ID, LARK_APP_SECRET   required
    LARK_DOMAIN                    optional, default https://open.larksuite.com
    BASE_APP_TOKEN                 required, the Base's app_token (the /base/<token> part of its URL)
    TABLE_ID                       required, the table to process (tbl...)
    LARK_UTC_OFFSET_HOURS          optional, default 8. Date fields are timestamps at local
                                   midnight; this is the offset used to turn them into dates.

Prerequisites in the Lark developer console and the Base:
    * the app has the `bitable:app` scope (tenant token scopes) and a released version
    * the app was added to the Base as a document app with edit rights.
      Scopes decide which APIs the app may call; this decides which Bases it may touch.

CLI:
    python lark_base.py list      # row count, missing columns, and the ticked rows
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_DOMAIN = "https://open.larksuite.com"

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


# Column names in the grid. Change them here if the grid is renamed.
FIELD_DATE = "Data Collection Date"
FIELD_SUBCAMPAIGN = "Sub-campaign (Doc Class 6)"
FIELD_ATTACHMENT = "QA Guidelines/PhoneMon (Bank)"
FIELD_PROCESS = "Process Row?"
FIELD_STATUS = "Status"
FIELD_OUTPUT = "Output CSV for NLU"
EXPECTED_FIELDS = (FIELD_DATE, FIELD_SUBCAMPAIGN, FIELD_ATTACHMENT, FIELD_PROCESS, FIELD_STATUS, FIELD_OUTPUT)

TOKEN_SLACK_SECONDS = 300
# The media download and upload APIs each allow 5 requests per second; stay below that.
DOWNLOAD_MIN_INTERVAL = 0.25
# The simple upload call accepts files up to 20 MB.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Lark error codes for a missing, invalid or expired access token.
INVALID_TOKEN_CODES = {99991661, 99991663, 99991668}
# Error codes that usually mean the app has no access to the Base.
PERMISSION_CODES = {91403, 99991672, 99991679}
PERMISSION_HINT = (
    "Check that the app has the `bitable:app` scope (with a released version) "
    "and was added to the Base as an editor."
)


class ConfigError(RuntimeError):
    """Missing or invalid configuration."""


class LarkApiError(RuntimeError):
    """The Lark API returned an error, or a response that is not JSON."""

    def __init__(self, code: int | None, msg: str, http_status: int | None = None):
        self.code = code
        self.msg = msg
        self.http_status = http_status
        hint = ""
        if http_status == 403 or code in PERMISSION_CODES:
            hint = f" {PERMISSION_HINT}"
        super().__init__(f"Lark API error {code} (HTTP {http_status}): {msg}.{hint}")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        raise LarkApiError(None, f"non-JSON response: {resp.text[:200]!r}", resp.status_code) from None
    if not isinstance(data, dict):
        raise LarkApiError(None, f"unexpected response: {resp.text[:200]!r}", resp.status_code)
    return data


# -- authentication ----------------------------------------------------------


class TenantTokenProvider:
    """Fetches and caches a tenant_access_token, refreshing it shortly before it expires."""

    def __init__(self, app_id: str, app_secret: str, domain: str = DEFAULT_DOMAIN,
                 http: httpx.Client | None = None):
        if not app_id or not app_secret:
            raise ConfigError("LARK_APP_ID and LARK_APP_SECRET are required.")
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            stale = self._token is None or self._expires_at - time.time() <= TOKEN_SLACK_SECONDS
            if force_refresh or stale:
                self._fetch()
            assert self._token is not None
            return self._token

    def _fetch(self) -> None:
        resp = self._http.post(
            f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        data = _json(resp)
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise LarkApiError(data.get("code"), str(data.get("msg") or "token request failed"),
                               resp.status_code)
        self._token = data["tenant_access_token"]
        self._expires_at = time.time() + int(data.get("expire") or 0)


# -- Base client -------------------------------------------------------------


class BaseClient:
    """Reads and updates records of one table. `tokens` needs a `get(force_refresh=False)` method."""

    def __init__(self, tokens: Any, app_token: str, table_id: str, domain: str = DEFAULT_DOMAIN,
                 http: httpx.Client | None = None):
        self._tokens = tokens
        self.app_token = app_token
        self.table_id = table_id
        self._domain = domain.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0)
        self._last_media_call = 0.0
        self._media_parent: str | None = None

    def _upload_target(self) -> str:
        """The token an upload must name as its parent.

        A Base opened from a wiki is addressed by its wiki node token in the records API, but
        uploads need the Base's own token, which the wiki node lookup returns. A token that is
        not a wiki node (or a failed lookup) is used as given.
        """
        if self._media_parent is None:
            self._media_parent = self.app_token
            try:
                node = self._call("GET", "/wiki/v2/spaces/get_node", params={"token": self.app_token}).get("node") or {}
                if node.get("obj_type") == "bitable" and node.get("obj_token"):
                    self._media_parent = str(node["obj_token"])
            except (LarkApiError, httpx.HTTPError):
                pass
        return self._media_parent

    def _throttle_media(self) -> None:
        wait = self._last_media_call + DOWNLOAD_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_media_call = time.monotonic()

    @property
    def _table_path(self) -> str:
        return f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}"

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Call the API and return the `data` object. Retries once with a fresh token."""
        url = f"{self._domain}/open-apis{path}"
        for attempt in (0, 1):
            token = self._tokens.get(force_refresh=attempt == 1)
            resp = self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
            data = _json(resp)
            code = data.get("code")
            if attempt == 0 and (resp.status_code == 401 or code in INVALID_TOKEN_CODES):
                continue
            if code != 0:
                raise LarkApiError(code, str(data.get("msg") or "request failed"), resp.status_code)
            return data.get("data") or {}
        raise AssertionError("unreachable")  # pragma: no cover

    def list_records(self, page_size: int = 500, view_id: str | None = None) -> list[dict[str, Any]]:
        """All records of the table, following pagination."""
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if view_id:
                params["view_id"] = view_id
            if page_token:
                params["page_token"] = page_token
            data = self._call("GET", f"{self._table_path}/records", params=params)
            records.extend(data.get("items") or [])
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                return records

    def list_fields(self) -> list[dict[str, Any]]:
        """Column definitions of the table (field_name, type, ...)."""
        fields: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._call("GET", f"{self._table_path}/fields", params=params)
            fields.extend(data.get("items") or [])
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                return fields

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Incremental update: only the given fields change."""
        data = self._call("PUT", f"{self._table_path}/records/{record_id}", json={"fields": fields})
        return data.get("record") or {}

    def upload_media(self, file_name: str, content: bytes) -> str:
        """Upload a file into this Base and return its file token.

        Put the token in an attachment column with `update_record(id, {column: [{"file_token": token}]})`.
        Lark's simple upload takes files up to 20 MB and 5 requests per second.
        """
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"{file_name} is {len(content)} bytes; Lark's upload accepts at most 20 MB")
        self._throttle_media()
        parent = self._upload_target()
        try:
            data = self._call(
                "POST", "/drive/v1/medias/upload_all",
                data={"file_name": file_name, "parent_type": "bitable_file",
                      "parent_node": parent, "size": str(len(content)),
                      "extra": json.dumps({"drive_route_token": parent})},
                files={"file": (file_name, content, "text/csv")},
            )
        except LarkApiError as exc:
            if exc.code == 1061044:  # parent node not found
                raise LarkApiError(exc.code, f"{exc.msg}. If the Base is opened from a wiki, the app may "
                                   "lack access to read the wiki node that holds the Base's own token",
                                   exc.http_status) from exc
            raise
        token = data.get("file_token")
        if not token:
            raise LarkApiError(None, "the upload returned no file_token")
        return str(token)

    def download_media(self, file_token: str, dest: Path, extra: str | None = None) -> Path:
        """Download an attachment's bytes to `dest`. Throttled to stay under 5 requests per second.

        `extra` is only needed for Bases with advanced permissions (otherwise Lark answers 403).
        """
        self._throttle_media()

        url = f"{self._domain}/open-apis/drive/v1/medias/{file_token}/download"
        params = {"extra": extra} if extra else None
        for attempt in (0, 1):
            token = self._tokens.get(force_refresh=attempt == 1)
            with self._http.stream("GET", url, params=params,
                                   headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status_code == 200:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as handle:
                        for chunk in resp.iter_bytes():
                            handle.write(chunk)
                    return dest
                body = resp.read()
                status = resp.status_code
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            code = data.get("code") if isinstance(data, dict) else None
            if attempt == 0 and (status == 401 or code in INVALID_TOKEN_CODES):
                continue
            msg = str((data.get("msg") if isinstance(data, dict) else None) or f"download refused (HTTP {status})")
            if status == 403:
                msg += "; a Base with advanced permissions also needs the `extra` parameter"
            raise LarkApiError(code, msg, status)
        raise AssertionError("unreachable")  # pragma: no cover


# -- value helpers -----------------------------------------------------------


def field_text(value: Any) -> str:
    """Plain text of a field value: a string, a rich-text segment list, or an option object."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    if isinstance(value, list):
        return "".join(field_text(part) for part in value)
    return str(value)


def ms_to_date(ms: int | float) -> date:
    """Convert a Lark date field (milliseconds since the epoch) to a date at the configured offset."""
    offset = timezone(timedelta(hours=float(os.environ.get("LARK_UTC_OFFSET_HOURS", "8"))))
    return datetime.fromtimestamp(ms / 1000, tz=offset).date()


def missing_fields(fields: list[dict[str, Any]], expected: tuple[str, ...] = EXPECTED_FIELDS) -> list[str]:
    present = {field_text(f.get("field_name")) for f in fields}
    return [name for name in expected if name not in present]


# -- configuration -----------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    domain: str
    app_token: str
    table_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ
        values = {
            "LARK_APP_ID": env.get("LARK_APP_ID", ""),
            "LARK_APP_SECRET": env.get("LARK_APP_SECRET", ""),
            "BASE_APP_TOKEN": env.get("BASE_APP_TOKEN", ""),
            "TABLE_ID": env.get("TABLE_ID", ""),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            app_id=values["LARK_APP_ID"],
            app_secret=values["LARK_APP_SECRET"],
            domain=env.get("LARK_DOMAIN", DEFAULT_DOMAIN),
            app_token=values["BASE_APP_TOKEN"],
            table_id=values["TABLE_ID"],
        )


def client_from_env() -> BaseClient:
    """A client that acts as the app itself (tenant access token)."""
    settings = Settings.from_env()
    mode = os.environ.get("LARK_AUTH", "tenant").strip().lower()
    if mode != "tenant":  # user mode was removed; do not silently act as the app instead
        raise ConfigError(f"LARK_AUTH={mode!r} is no longer supported. Remove it; the app acts as itself.")
    tokens = TenantTokenProvider(settings.app_id, settings.app_secret, settings.domain)
    return BaseClient(tokens, settings.app_token, settings.table_id, settings.domain)


# -- CLI ---------------------------------------------------------------------


def _describe_row(record: dict[str, Any]) -> str:
    fields = record.get("fields") or {}
    raw_date = fields.get(FIELD_DATE)
    shown_date = ms_to_date(raw_date).isoformat() if isinstance(raw_date, (int, float)) else "-"
    return f"{record.get('record_id')}  {shown_date}  {field_text(fields.get(FIELD_SUBCAMPAIGN)) or '-'}"


def cmd_list(client: BaseClient) -> int:
    missing = missing_fields(client.list_fields())
    if missing:
        print("Missing columns (add them or fix the names at the top of lark_base.py):")
        for name in missing:
            print(f"  - {name}")
    records = client.list_records()
    ticked = [r for r in records if (r.get("fields") or {}).get(FIELD_PROCESS) is True]
    print(f"{len(records)} rows, {len(ticked)} with '{FIELD_PROCESS}' ticked.")
    for record in ticked:
        print("  " + _describe_row(record))
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lark Base client.")
    parser.add_subparsers(dest="command", required=True).add_parser(
        "list", help="show row count, missing columns and ticked rows")
    parser.parse_args(argv)
    try:
        return cmd_list(client_from_env())
    except (ConfigError, LarkApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
