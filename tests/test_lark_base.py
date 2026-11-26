import json
import os
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

import lark_base
from lark_base import BaseClient, ConfigError, LarkApiError, TenantTokenProvider

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
RECORDS_PATH = "/open-apis/bitable/v1/apps/APP/tables/TBL/records"


def http_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def token_response(token="t-1", expire=7200):
    return httpx.Response(200, json={"code": 0, "msg": "ok", "tenant_access_token": token, "expire": expire})


class FakeTokens:
    def __init__(self):
        self.calls = []

    def get(self, force_refresh=False):
        self.calls.append(force_refresh)
        return "fresh" if force_refresh else "stale"


# -- TenantTokenProvider -----------------------------------------------------


def test_token_is_cached_until_close_to_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(lark_base, "time", SimpleNamespace(time=lambda: clock[0]))
    fetches = []

    def handler(request):
        assert request.url.path == TOKEN_PATH
        assert json.loads(request.content) == {"app_id": "id", "app_secret": "secret"}
        fetches.append(1)
        return token_response(token=f"t-{len(fetches)}", expire=7200)

    provider = TenantTokenProvider("id", "secret", http=http_client(handler))
    assert provider.get() == "t-1"
    clock[0] += 3000
    assert provider.get() == "t-1"  # still valid
    assert len(fetches) == 1
    clock[0] += 3900  # 6900s in: fewer than 300s left
    assert provider.get() == "t-2"
    assert provider.get(force_refresh=True) == "t-3"


def test_token_error_raises_with_code():
    handler = lambda request: httpx.Response(200, json={"code": 10003, "msg": "invalid param"})
    provider = TenantTokenProvider("id", "secret", http=http_client(handler))
    with pytest.raises(LarkApiError) as exc:
        provider.get()
    assert exc.value.code == 10003


def test_token_provider_requires_credentials():
    with pytest.raises(ConfigError):
        TenantTokenProvider("", "")


# -- BaseClient --------------------------------------------------------------


def test_list_records_follows_pagination():
    seen = []

    def handler(request):
        assert request.url.path == RECORDS_PATH
        assert request.headers["Authorization"] == "Bearer stale"
        seen.append(dict(request.url.params))
        if "page_token" not in request.url.params:
            data = {"items": [{"record_id": "r1"}], "has_more": True, "page_token": "p2"}
        else:
            data = {"items": [{"record_id": "r2"}], "has_more": False}
        return httpx.Response(200, json={"code": 0, "data": data})

    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    records = client.list_records(page_size=1)
    assert [r["record_id"] for r in records] == ["r1", "r2"]
    assert seen[0] == {"page_size": "1"}
    assert seen[1] == {"page_size": "1", "page_token": "p2"}


def test_list_records_empty_table():
    handler = lambda request: httpx.Response(200, json={"code": 0, "data": {"has_more": False, "total": 0}})
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    assert client.list_records() == []


def test_retries_once_with_fresh_token_when_token_is_invalid():
    tokens = FakeTokens()
    auth_headers = []

    def handler(request):
        auth_headers.append(request.headers["Authorization"])
        if len(auth_headers) == 1:
            return httpx.Response(200, json={"code": 99991663, "msg": "Invalid access token"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    client = BaseClient(tokens, "APP", "TBL", http=http_client(handler))
    assert client.list_records() == []
    assert auth_headers == ["Bearer stale", "Bearer fresh"]
    assert tokens.calls == [False, True]


def test_does_not_retry_forever_on_invalid_token():
    handler = lambda request: httpx.Response(200, json={"code": 99991663, "msg": "Invalid access token"})
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    with pytest.raises(LarkApiError) as exc:
        client.list_records()
    assert exc.value.code == 99991663


def test_permission_error_includes_hint():
    handler = lambda request: httpx.Response(403, json={"code": 91403, "msg": "Forbidden"})
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    with pytest.raises(LarkApiError) as exc:
        client.list_records()
    assert "91403" in str(exc.value)
    assert "read access to the Base" in str(exc.value)


def test_non_json_response_is_an_api_error():
    handler = lambda request: httpx.Response(502, text="<html>bad gateway</html>")
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    with pytest.raises(LarkApiError) as exc:
        client.list_records()
    assert exc.value.http_status == 502


def test_list_fields_and_missing_fields():
    items = [{"field_name": name} for name in (lark_base.FIELD_DATE, lark_base.FIELD_PROCESS)]
    handler = lambda request: httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": False}})
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    missing = lark_base.missing_fields(client.list_fields())
    assert missing == [lark_base.FIELD_SUBCAMPAIGN, lark_base.FIELD_ATTACHMENT]


def test_cmd_list_prints_ticked_rows_and_reports_missing_columns(capsys):
    fields = [{"field_name": name} for name in lark_base.EXPECTED_FIELDS]
    records = [
        {"record_id": "r1", "fields": {lark_base.FIELD_SUBCAMPAIGN: "Class A", lark_base.FIELD_PROCESS: True}},
        {"record_id": "r2", "fields": {lark_base.FIELD_SUBCAMPAIGN: "Class B"}},
    ]

    def handler(request):
        if request.url.path.endswith("/fields"):
            return httpx.Response(200, json={"code": 0, "data": {"items": fields, "has_more": False}})
        return httpx.Response(200, json={"code": 0, "data": {"items": records, "has_more": False}})

    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    assert lark_base.cmd_list(client) == 0
    out = capsys.readouterr().out
    assert "2 rows, 1 with 'Process Row?' ticked." in out
    assert "r1" in out and "Class A" in out
    assert "r2" not in out


# -- helpers -----------------------------------------------------------------


def test_field_text_handles_rich_text_options_and_empty():
    ft = lark_base.field_text
    assert ft(None) == ""
    assert ft("plain") == "plain"
    assert ft([{"text": "a", "type": "text"}, {"text": "b", "type": "text"}]) == "ab"
    assert ft({"name": "Opt"}) == "Opt"
    assert ft(42) == "42"


def test_ms_to_date_uses_configured_offset(monkeypatch):
    # 2026-03-01 00:00 at UTC+8 is 2026-02-28 16:00 UTC.
    ms = 1772294400000
    monkeypatch.delenv("LARK_UTC_OFFSET_HOURS", raising=False)
    assert lark_base.ms_to_date(ms) == date(2026, 3, 1)
    monkeypatch.setenv("LARK_UTC_OFFSET_HOURS", "0")
    assert lark_base.ms_to_date(ms) == date(2026, 2, 28)


def test_settings_reports_all_missing_variables(monkeypatch):
    monkeypatch.setattr(lark_base, "load_dotenv", lambda *a, **k: None)
    for name in ("LARK_APP_ID", "LARK_APP_SECRET", "BASE_APP_TOKEN", "TABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as exc:
        lark_base.Settings.from_env()
    for name in ("LARK_APP_ID", "LARK_APP_SECRET", "BASE_APP_TOKEN", "TABLE_ID"):
        assert name in str(exc.value)


def _set_env(monkeypatch, **extra):
    monkeypatch.setattr(lark_base, "load_dotenv", lambda *a, **k: None)
    values = {"LARK_APP_ID": "id", "LARK_APP_SECRET": "secret", "BASE_APP_TOKEN": "APP", "TABLE_ID": "TBL"}
    for name, value in {**values, **extra}.items():
        monkeypatch.setenv(name, value)


def test_client_from_env_defaults_to_the_tenant_token(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("LARK_AUTH", raising=False)
    client = lark_base.client_from_env()
    assert isinstance(client._tokens, TenantTokenProvider)


def test_client_from_env_accepts_an_explicit_tenant_setting(monkeypatch):
    _set_env(monkeypatch, LARK_AUTH="Tenant")
    assert isinstance(lark_base.client_from_env()._tokens, TenantTokenProvider)


@pytest.mark.parametrize("mode", ["user", "both"])
def test_client_from_env_rejects_other_auth_modes(monkeypatch, mode):
    _set_env(monkeypatch, LARK_AUTH=mode)
    with pytest.raises(ConfigError) as exc:
        lark_base.client_from_env()
    assert "LARK_AUTH" in str(exc.value)


def test_load_dotenv_sets_missing_values_and_keeps_existing_ones(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nNEW_ONE="quoted value"\nexport OTHER=plain\nKEPT=from-file\n', encoding="utf-8")
    monkeypatch.delenv("NEW_ONE", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.setenv("KEPT", "from-environment")
    lark_base.load_dotenv(env_file)
    assert os.environ["NEW_ONE"] == "quoted value"
    assert os.environ["OTHER"] == "plain"
    assert os.environ["KEPT"] == "from-environment"
    monkeypatch.delenv("NEW_ONE")
    monkeypatch.delenv("OTHER")


# -- download_media ----------------------------------------------------------

DOWNLOAD_PATH = "/open-apis/drive/v1/medias/tokX/download"


def test_download_media_writes_the_bytes(tmp_path):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, content=b"\x00\x01binary")

    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    dest = client.download_media("tokX", tmp_path / "sub" / "f.bin")
    assert dest.read_bytes() == b"\x00\x01binary"
    assert seen == {"path": DOWNLOAD_PATH, "auth": "Bearer stale", "params": {}}


def test_download_media_sends_extra_when_given(tmp_path):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, content=b"x")

    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    client.download_media("tokX", tmp_path / "f", extra='{"bitablePerm":{}}')
    assert seen["params"] == {"extra": '{"bitablePerm":{}}'}


def test_download_media_403_explains_the_extra_parameter(tmp_path):
    handler = lambda request: httpx.Response(403, content=b"")
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    with pytest.raises(LarkApiError) as exc:
        client.download_media("tokX", tmp_path / "f")
    assert exc.value.http_status == 403
    assert "extra" in str(exc.value)
    assert not (tmp_path / "f").exists()


def test_download_media_retries_once_with_a_fresh_token(tmp_path):
    tokens = FakeTokens()
    auth = []

    def handler(request):
        auth.append(request.headers["Authorization"])
        if len(auth) == 1:
            return httpx.Response(400, json={"code": 99991663, "msg": "Invalid access token"})
        return httpx.Response(200, content=b"ok")

    client = BaseClient(tokens, "APP", "TBL", http=http_client(handler))
    client.download_media("tokX", tmp_path / "f")
    assert auth == ["Bearer stale", "Bearer fresh"]
    assert (tmp_path / "f").read_bytes() == b"ok"


def test_download_media_error_body_is_reported(tmp_path):
    handler = lambda request: httpx.Response(404, json={"code": 1061003, "msg": "not found."})
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))
    with pytest.raises(LarkApiError) as exc:
        client.download_media("tokX", tmp_path / "f")
    assert exc.value.code == 1061003


def test_download_media_is_throttled_below_five_requests_per_second(monkeypatch, tmp_path):
    clock = [100.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(lark_base, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=fake_sleep, time=lambda: clock[0]))
    handler = lambda request: httpx.Response(200, content=b"x")
    client = BaseClient(FakeTokens(), "APP", "TBL", http=http_client(handler))

    client.download_media("tokX", tmp_path / "a")  # first call: no wait
    client.download_media("tokX", tmp_path / "b")  # immediately after: must wait the full interval
    assert sleeps == [pytest.approx(lark_base.DOWNLOAD_MIN_INTERVAL)]
