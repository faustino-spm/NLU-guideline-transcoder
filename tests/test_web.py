import importlib
import sys
import threading
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import web
from fakes import FakeClient, record
from lark_base import ConfigError, LarkApiError
from llm import StubLLM

KEY = "s3cret"


class CountingLLM:
    def __init__(self, csv_text: str = "a,b\n1,2\n"):
        self.calls = 0
        self.csv_text = csv_text

    def generate_csv(self, row_date, sub_campaign, files):
        self.calls += 1
        return self.csv_text


class BlockingLLM(CountingLLM):
    """Holds a run open until the test releases it."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def generate_csv(self, row_date, sub_campaign, files):
        super().generate_csv(row_date, sub_campaign, files)
        self.started.set()
        assert self.release.wait(5), "test never released the run"
        return "a,b\n1,2\n"


class DownClient(FakeClient):
    def list_records(self, **_):
        raise LarkApiError(91403, "Forbidden", 403)


def make(records, llm=None, run_key=KEY, client=None, **kwargs):
    client = client or FakeClient(records)
    llm = llm or CountingLLM()
    return TestClient(web.create_app(client, llm, run_key, **kwargs)), client, llm


# -- key check ---------------------------------------------------------------


@pytest.mark.parametrize("method, path", [("get", "/run"), ("post", "/run"), ("get", "/status")])
@pytest.mark.parametrize("params", [{}, {"key": "wrong"}, {"key": ""}])
def test_every_page_needs_the_key(method, path, params):
    site, client, llm = make([record("r1")])
    response = getattr(site, method)(path, params=params, follow_redirects=False)
    assert response.status_code == 403
    assert client.downloads == [] and llm.calls == 0


def test_the_app_refuses_to_start_without_a_run_key():
    with pytest.raises(ConfigError):
        web.create_app(FakeClient([]), StubLLM(), "")


def test_api_docs_are_not_exposed():
    site, *_ = make([])
    assert site.get("/docs").status_code == 404
    assert site.get("/openapi.json").status_code == 404


# -- confirmation page -------------------------------------------------------


def test_get_run_lists_ticked_rows_and_changes_nothing():
    site, client, llm = make([record("r1"), record("r2", ticked=False), record("r3")])
    response = site.get("/run", params={"key": KEY})

    assert response.status_code == 200
    assert "r1" in response.text and "r3" in response.text and "r2" not in response.text
    assert "Run 2 row(s)" in response.text
    assert 'method="post"' in response.text
    assert client.downloads == [] and llm.calls == 0  # a link preview or double click starts nothing


def test_get_run_with_nothing_ticked_has_no_run_button():
    site, *_ = make([record("r1", ticked=False)])
    text = site.get("/run", params={"key": KEY}).text
    assert "No rows are ticked" in text
    assert "<button" not in text


def test_the_key_is_url_encoded_in_links_and_the_form_action():
    site, *_ = make([record("r1")], run_key="k&1 x")
    get_text = site.get("/run", params={"key": "k&1 x"}).text
    assert 'action="/run?key=k%261%20x"' in get_text
    assert 'href="/status?key=k%261%20x"' in get_text
    post_text = site.post("/run", params={"key": "k&1 x"}).text
    assert 'href="/run?key=k%261%20x"' in post_text
    assert 'href="/status?key=k%261%20x"' in post_text


def test_row_data_is_html_escaped():
    site, *_ = make([record("r1", sub_campaign="<script>alert(1)</script>")])
    text = site.get("/run", params={"key": KEY}).text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_an_unreadable_grid_gives_502_with_the_reason():
    site, *_ = make([], client=DownClient([]))
    response = site.get("/run", params={"key": KEY})
    assert response.status_code == 502
    assert "91403" in response.text


# -- running -----------------------------------------------------------------


def test_post_run_processes_ticked_rows_and_embeds_downloadable_csv():
    site, client, llm = make([record("r1"), record("r2", ticked=False)], llm=CountingLLM("col1,col2\n<v>,2\n"))
    response = site.post("/run", params={"key": KEY})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert llm.calls == 1
    assert client.downloads == ["tok-r1"]
    assert "2026-03-01_r1.csv" in response.text
    assert "data:text/csv;charset=utf-8," in response.text
    uri = response.text.split('href="data:text/csv;charset=utf-8,', 1)[1].split('"', 1)[0]
    assert unquote(uri) == "col1,col2\n<v>,2\n"
    assert "&lt;v&gt;,2" in response.text
    assert "Done: 1 succeeded, 0 failed." in response.text
    assert "not reached" not in response.text
    assert response.text.rstrip().endswith("</html>")


def test_post_run_with_nothing_ticked_says_so():
    site, client, llm = make([record("r1", ticked=False)])
    text = site.post("/run", params={"key": KEY}).text
    assert "No rows were ticked." in text
    assert client.downloads == [] and llm.calls == 0


def test_rows_not_reached_before_the_budget_ends_are_reported():
    site, _, llm = make([record("r1"), record("r2")], budget_seconds=0)
    text = site.post("/run", params={"key": KEY}).text
    assert llm.calls == 0
    assert "2 ticked row(s) were not reached" in text


def test_a_failed_row_is_shown_with_its_reason_and_counted():
    site, _, _ = make([record("r1", attachments=None), record("r2")])
    text = site.post("/run", params={"key": KEY}).text
    assert "no attachment" in text
    assert "Done: 1 succeeded, 1 failed." in text


def test_a_run_that_cannot_read_the_grid_says_so_and_releases_the_lock():
    broken = DownClient([])
    site, *_ = make([], client=broken)
    text = site.post("/run", params={"key": KEY}).text
    assert "Run failed" in text and "91403" in text
    assert "already in progress" not in site.post("/run", params={"key": KEY}).text


def test_a_second_run_in_the_same_process_is_refused_while_one_is_going():
    llm = BlockingLLM()
    site, _, _ = make([record("r1")], llm=llm)
    other = TestClient(site.app)
    first = {}
    runner = threading.Thread(target=lambda: first.update(text=site.post("/run", params={"key": KEY}).text))
    runner.start()
    try:
        assert llm.started.wait(5)
        second = other.post("/run", params={"key": KEY}).text
        assert "already in progress" in second
    finally:
        llm.release.set()
        runner.join(10)

    assert llm.calls == 1
    assert "Done: 1 succeeded, 0 failed." in first["text"]
    # once finished, a new run can start again
    assert "Done: 1 succeeded" in site.post("/run", params={"key": KEY}).text


# -- status page -------------------------------------------------------------


def test_status_with_nothing_processed():
    site, *_ = make([record("r1")])
    assert "No row has been processed yet." in site.get("/status", params={"key": KEY}).text


def test_status_retains_processed_rows_and_csv_downloads():
    site, *_ = make([record("r1", sub_campaign="<b>x</b>"), record("r2", attachments=None)],
                    llm=CountingLLM("a,b\n1,2\n"))
    site.post("/run", params={"key": KEY})
    text = site.get("/status", params={"key": KEY}).text
    assert "r1" in text and "2026-03-01" in text and "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<b>x</b>" not in text
    assert "2026-03-01_r1.csv" in text
    assert "data:text/csv;charset=utf-8," in text
    assert "r2" in text and "Failed" in text and "no attachment" in text


# -- configuration and entrypoint --------------------------------------------


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(web, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(web, "client_from_env", lambda: FakeClient([record("r1")]))
    monkeypatch.setattr(web, "llm_from_env", lambda: CountingLLM())
    monkeypatch.setenv("RUN_KEY", KEY)
    monkeypatch.delenv("RUN_BUDGET_SECONDS", raising=False)
    return monkeypatch


def test_app_from_env_builds_a_working_app(env):
    site = TestClient(web.app_from_env())
    assert "Run 1 row(s)" in site.get("/run", params={"key": KEY}).text


def test_app_from_env_reads_the_budget(env):
    env.setenv("RUN_BUDGET_SECONDS", "0.5")
    captured = {}
    real = web.create_app
    env.setattr(web, "create_app", lambda *args: captured.update(budget=args[3]) or real(*args))
    web.app_from_env()
    assert captured["budget"] == 0.5


def test_app_from_env_defaults_the_budget(env):
    captured = {}
    real = web.create_app
    env.setattr(web, "create_app", lambda *args: captured.update(budget=args[3]) or real(*args))
    web.app_from_env()
    assert captured["budget"] == web.DEFAULT_BUDGET_SECONDS


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_app_from_env_rejects_a_bad_budget(env, value):
    env.setenv("RUN_BUDGET_SECONDS", value)
    with pytest.raises(ConfigError, match="RUN_BUDGET_SECONDS"):
        web.app_from_env()


def test_app_from_env_needs_the_run_key(env):
    env.delenv("RUN_KEY")
    with pytest.raises(ConfigError, match="RUN_KEY"):
        web.app_from_env()


def test_app_py_exposes_the_app_that_web_builds(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(web, "app_from_env", lambda: sentinel)
    monkeypatch.delitem(sys.modules, "app", raising=False)
    try:
        assert importlib.import_module("app").app is sentinel
    finally:
        sys.modules.pop("app", None)
