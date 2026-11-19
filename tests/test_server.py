import threading

import pytest
from fastapi.testclient import TestClient

import server
from fakes import FakeClient, record
from lark_base import FIELD_PROCESS, FIELD_STATUS, ConfigError, LarkApiError
from llm import StubLLM

KEY = "s3cret"


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def generate_csv(self, row_date, sub_campaign, files):
        self.calls += 1
        return "a,b\n1,2\n"


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


def make(records, llm=None, tmp_path=None, run_key=KEY, client=None):
    client = client or FakeClient(records)
    llm = llm or CountingLLM()
    app = server.create_app(client, llm, run_key, output_dir=tmp_path / "output")
    return TestClient(app), client, llm, app.state.runs


# -- key check ---------------------------------------------------------------


@pytest.mark.parametrize("method, path", [("get", "/run"), ("post", "/run"), ("get", "/status")])
@pytest.mark.parametrize("params", [{}, {"key": "wrong"}, {"key": ""}])
def test_every_page_needs_the_key(tmp_path, method, path, params):
    web, client, llm, runs = make([record("r1")], tmp_path=tmp_path)
    response = getattr(web, method)(path, params=params, follow_redirects=False)
    assert response.status_code == 403
    assert runs.snapshot() == (False, None)
    assert client.updates == [] and llm.calls == 0


def test_the_app_refuses_to_start_without_a_run_key(tmp_path):
    with pytest.raises(ConfigError):
        server.create_app(FakeClient([]), StubLLM(), "", output_dir=tmp_path)


def test_api_docs_are_not_exposed(tmp_path):
    web, *_ = make([], tmp_path=tmp_path)
    assert web.get("/docs").status_code == 404
    assert web.get("/openapi.json").status_code == 404


# -- confirmation page -------------------------------------------------------


def test_get_run_lists_ticked_rows_and_changes_nothing(tmp_path):
    web, client, llm, runs = make([record("r1"), record("r2", ticked=False), record("r3")], tmp_path=tmp_path)
    response = web.get("/run", params={"key": KEY})

    assert response.status_code == 200
    assert "r1" in response.text and "r3" in response.text and "r2" not in response.text
    assert "Run 2 row(s)" in response.text
    assert 'method="post"' in response.text
    assert runs.snapshot() == (False, None)  # a link preview or double click starts nothing
    assert client.updates == [] and llm.calls == 0


def test_get_run_with_nothing_ticked_has_no_run_button(tmp_path):
    web, *_ = make([record("r1", ticked=False)], tmp_path=tmp_path)
    text = web.get("/run", params={"key": KEY}).text
    assert "No rows are ticked" in text
    assert "<button" not in text


def test_the_key_is_url_encoded_in_the_form_action(tmp_path):
    web, *_ = make([record("r1")], tmp_path=tmp_path, run_key="k&1 x")
    text = web.get("/run", params={"key": "k&1 x"}).text
    assert 'action="/run?key=k%261%20x"' in text


def test_row_data_is_html_escaped(tmp_path):
    web, *_ = make([record("r1", sub_campaign="<script>alert(1)</script>")], tmp_path=tmp_path)
    text = web.get("/run", params={"key": KEY}).text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_an_unreadable_grid_gives_502_with_the_reason(tmp_path):
    class DownClient(FakeClient):
        def list_records(self, **_):
            raise LarkApiError(91403, "Forbidden", 403)

    web, *_ = make([], tmp_path=tmp_path, client=DownClient([]))
    response = web.get("/run", params={"key": KEY})
    assert response.status_code == 502
    assert "91403" in response.text


# -- starting a run ----------------------------------------------------------


def test_post_run_redirects_to_status_and_processes_the_rows(tmp_path):
    web, client, llm, runs = make([record("r1"), record("r2", ticked=False)], tmp_path=tmp_path)
    response = web.post("/run", params={"key": KEY}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/status?key={KEY}"

    runs.wait(5)
    assert llm.calls == 1
    assert client.records["r1"]["fields"][FIELD_STATUS] == "Integrated"
    assert client.records["r1"]["fields"][FIELD_PROCESS] is False
    assert client.records["r2"]["fields"][FIELD_STATUS] == "Not Processed"
    assert (tmp_path / "output" / "2026-03-01_r1.csv").is_file()

    page = web.get("/status", params={"key": KEY}).text
    assert "Finished at" in page and "r1" in page and "Integrated" in page
    assert 'http-equiv="refresh"' not in page


def test_a_second_post_during_a_run_is_rejected_and_only_one_run_happens(tmp_path):
    llm = BlockingLLM()
    web, client, _, runs = make([record("r1")], llm=llm, tmp_path=tmp_path)

    assert web.post("/run", params={"key": KEY}, follow_redirects=False).status_code == 303
    assert llm.started.wait(5)

    second = web.post("/run", params={"key": KEY}, follow_redirects=False)
    assert second.status_code == 409
    assert "already in progress" in second.text
    assert "A run is in progress" in web.get("/run", params={"key": KEY}).text
    status_page = web.get("/status", params={"key": KEY}).text
    assert "Running since" in status_page and 'http-equiv="refresh"' in status_page

    llm.release.set()
    runs.wait(5)
    assert llm.calls == 1
    assert runs.snapshot()[0] is False
    # once finished, a new run can start again
    assert web.post("/run", params={"key": KEY}, follow_redirects=False).status_code == 303
    runs.wait(5)


def test_a_run_that_fails_as_a_whole_shows_its_error(tmp_path):
    class DownClient(FakeClient):
        def list_records(self, **_):
            raise RuntimeError("network down")

    web, _, _, runs = make([], tmp_path=tmp_path, client=DownClient([]))
    assert web.post("/run", params={"key": KEY}, follow_redirects=False).status_code == 303
    runs.wait(5)
    page = web.get("/status", params={"key": KEY}).text
    assert "Run failed" in page and "network down" in page
    assert runs.snapshot()[0] is False


def test_status_before_any_run(tmp_path):
    web, *_ = make([], tmp_path=tmp_path)
    assert "No run has been started yet" in web.get("/status", params={"key": KEY}).text


def test_failed_rows_are_shown_with_their_reason(tmp_path):
    web, _, _, runs = make([record("r1", attachments=None)], tmp_path=tmp_path)
    web.post("/run", params={"key": KEY}, follow_redirects=False)
    runs.wait(5)
    page = web.get("/status", params={"key": KEY}).text
    assert "Failed" in page and "no attachment" in page
