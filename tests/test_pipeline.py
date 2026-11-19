import threading
from datetime import date

import pytest

import pipeline
from fakes import FakeClient, attachment, record
from lark_base import FIELD_PROCESS, FIELD_STATUS
from llm import StubLLM


def run(client, llm, tmp_path, **kwargs):
    """process_ticked_rows with a private lock and output folder, so tests never touch the repo."""
    kwargs.setdefault("lock", threading.Lock())
    return pipeline.process_ticked_rows(client, llm, output_dir=tmp_path / "output", **kwargs)


class RecordingLLM:
    def __init__(self, fail_for_sub_campaign: str | None = None, csv_text: str = "a,b\n1,2\n"):
        self.calls = []
        self.fail_for = fail_for_sub_campaign
        self.csv_text = csv_text

    def generate_csv(self, row_date, sub_campaign, files):
        # The files only exist during the call, so capture their contents now.
        self.calls.append((row_date, sub_campaign, [(f.name, f.read_bytes()) for f in files]))
        if sub_campaign == self.fail_for:
            raise ValueError("model exploded")
        return self.csv_text


# -- row model (Task 2) ------------------------------------------------------


def test_parse_row_reads_every_column():
    row = pipeline.parse_row(record("r1", attachments=[attachment("tok1", "a.pdf", 10, "application/pdf")]))
    assert row.record_id == "r1"
    assert row.date == date(2026, 3, 1)
    assert row.sub_campaign == "Class A"
    assert row.process is True
    assert row.status == "Not Processed"
    assert row.attachments == (pipeline.Attachment("tok1", "a.pdf", 10, "application/pdf"),)


def test_parse_row_tolerates_empty_cells():
    row = pipeline.parse_row(record("r2", ticked=False, status=None, sub_campaign=None, date_ms=None,
                                    attachments=None))
    assert row.date is None
    assert row.sub_campaign == ""
    assert row.status == ""
    assert row.attachments == ()
    assert row.process is False


def test_parse_attachments_skips_items_without_a_file_token():
    parsed = pipeline.parse_attachments([{"name": "x"}, attachment("ok"), "junk"])
    assert [a.file_token for a in parsed] == ["ok"]


def test_ticked_rows_returns_only_ticked_rows():
    client = FakeClient([record("r1"), record("r2", ticked=False), record("r3")])
    assert [r.record_id for r in pipeline.ticked_rows(client)] == ["r1", "r3"]


def test_dry_run_prints_ticked_rows_and_writes_nothing(capsys):
    client = FakeClient([
        record("r1", attachments=[attachment("tok1", "a.pdf", 10)]),
        record("r2", ticked=False),
        record("r3", attachments=None),
    ])
    assert pipeline.cmd_dry_run(client) == 0
    out = capsys.readouterr().out
    assert "2 row(s) would be processed." in out
    assert "r1" in out and "a.pdf (10 bytes)" in out
    assert "r3" in out and "no attachment" in out
    assert "r2" not in out
    assert client.updates == []
    assert client.downloads == []


# -- claim, process, finalize (Task 3) ---------------------------------------


def test_claim_then_integrate_in_order_and_leave_unticked_rows_alone(tmp_path):
    client = FakeClient([record("r1"), record("r2", ticked=False), record("r3")])
    report = run(client, RecordingLLM(), tmp_path)

    claim = {FIELD_STATUS: "Queued", FIELD_PROCESS: False}
    done = {FIELD_STATUS: "Integrated"}
    assert client.updates == [("r1", claim), ("r3", claim), ("r1", done), ("r3", done)]
    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Integrated"), ("r3", "Integrated")]
    assert report.finished_at is not None
    assert client.records["r2"]["fields"][FIELD_STATUS] == "Not Processed"


def test_a_failing_row_is_marked_failed_and_the_others_still_run(tmp_path):
    client = FakeClient([record("r1", sub_campaign="bad"), record("r2", sub_campaign="good")])
    report = run(client, RecordingLLM(fail_for_sub_campaign="bad"), tmp_path)

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Integrated")]
    assert "model exploded" in report.results[0].detail
    assert client.records["r1"]["fields"][FIELD_STATUS] == "Failed"
    assert client.records["r1"]["fields"][FIELD_PROCESS] is False
    assert client.records["r2"]["fields"][FIELD_STATUS] == "Integrated"


def test_a_row_that_cannot_be_claimed_stays_ticked_and_is_skipped(tmp_path):
    client = FakeClient([record("r1"), record("r2")], fail_updates_for={"r1"})
    llm = RecordingLLM()
    report = run(client, llm, tmp_path)

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Integrated")]
    assert "could not claim" in report.results[0].detail
    assert client.records["r1"]["fields"][FIELD_PROCESS] is True
    assert len(llm.calls) == 1


def test_processed_rows_are_not_picked_up_again(tmp_path):
    client = FakeClient([record("r1")])
    run(client, RecordingLLM(), tmp_path)
    assert run(client, RecordingLLM(), tmp_path).results == []


def test_a_second_run_is_rejected_while_one_is_in_progress(tmp_path):
    lock = threading.Lock()
    client = FakeClient([record("r1")])
    nested = {}

    class ReentrantLLM:
        def generate_csv(self, row_date, sub_campaign, files):
            with pytest.raises(pipeline.AlreadyRunning):
                run(client, StubLLM(), tmp_path, lock=lock)
            nested["rejected"] = True
            return "a\n1\n"

    report = run(client, ReentrantLLM(), tmp_path, lock=lock)
    assert nested == {"rejected": True}
    assert report.results[0].status == "Integrated"


def test_lock_is_released_when_the_run_itself_fails(tmp_path):
    lock = threading.Lock()

    class BrokenClient(FakeClient):
        def list_records(self, **_):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        run(BrokenClient([]), StubLLM(), tmp_path, lock=lock)
    assert lock.acquire(blocking=False)


def test_a_passed_in_report_is_filled_in_place(tmp_path):
    report = pipeline.RunReport()
    returned = run(FakeClient([record("r1")]), RecordingLLM(), tmp_path, report=report)
    assert returned is report
    assert len(report.results) == 1


def test_cmd_run_prints_a_summary_and_returns_1_when_a_row_failed(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # cmd_run saves into ./output
    client = FakeClient([record("r1", sub_campaign="bad"), record("r2")])
    code = pipeline.cmd_run(client, RecordingLLM(fail_for_sub_campaign="bad"))
    out = capsys.readouterr().out
    assert code == 1
    assert "Processed 2 row(s): 1 Integrated, 1 Failed." in out
    assert str(date(2026, 3, 1)) in out


# -- attachments and CSV output (Task 4) -------------------------------------


def test_llm_receives_the_rows_inputs_and_downloaded_files(tmp_path):
    client = FakeClient([record("r1", sub_campaign="Class B", attachments=[
        attachment("tokA", "one.pdf"), attachment("tokB", "two.docx")])])
    llm = RecordingLLM()
    run(client, llm, tmp_path)

    assert client.downloads == ["tokA", "tokB"]
    assert llm.calls == [(date(2026, 3, 1), "Class B",
                          [("1_one.pdf", b"bytes of tokA"), ("2_two.docx", b"bytes of tokB")])]


def test_csv_is_saved_as_date_and_record_id(tmp_path):
    client = FakeClient([record("rec123")])
    report = run(client, RecordingLLM(csv_text="x,y\n1,2\n"), tmp_path)

    expected = tmp_path / "output" / "2026-03-01_rec123.csv"
    assert report.results[0].output_path == str(expected)
    assert expected.read_text(encoding="utf-8") == "x,y\n1,2\n"


def test_rerunning_a_row_overwrites_its_csv(tmp_path):
    client = FakeClient([record("rec1")])
    run(client, RecordingLLM(csv_text="v\n1\n"), tmp_path)
    client.records["rec1"]["fields"][FIELD_PROCESS] = True
    run(client, RecordingLLM(csv_text="v\n2\n"), tmp_path)
    assert (tmp_path / "output" / "2026-03-01_rec1.csv").read_text(encoding="utf-8") == "v\n2\n"


def test_row_without_attachment_fails_without_calling_the_llm(tmp_path):
    client = FakeClient([record("r1", attachments=None)])
    llm = RecordingLLM()
    report = run(client, llm, tmp_path)

    assert report.results[0].status == "Failed"
    assert "no attachment" in report.results[0].detail
    assert llm.calls == []
    assert client.records["r1"]["fields"][FIELD_STATUS] == "Failed"
    assert not (tmp_path / "output").exists()


def test_download_error_marks_the_row_failed_and_saves_nothing(tmp_path):
    client = FakeClient([record("r1"), record("r2")], fail_downloads_for={"tok-r1"})
    report = run(client, RecordingLLM(), tmp_path)

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Integrated")]
    assert "download refused" in report.results[0].detail
    assert not (tmp_path / "output" / "2026-03-01_r1.csv").exists()
    assert (tmp_path / "output" / "2026-03-01_r2.csv").exists()


def test_empty_csv_from_the_llm_fails_the_row(tmp_path):
    client = FakeClient([record("r1")])
    report = run(client, RecordingLLM(csv_text="  \n"), tmp_path)
    assert report.results[0].status == "Failed"
    assert "empty CSV" in report.results[0].detail
    assert list((tmp_path).glob("output/*.csv")) == []


def test_stub_llm_output_is_a_valid_csv_describing_its_inputs(tmp_path):
    client = FakeClient([record("r1", attachments=[attachment("t", "guide.pdf")])])
    report = run(client, StubLLM(), tmp_path)
    text = open(report.results[0].output_path, encoding="utf-8").read()
    assert text.splitlines() == [
        "data_collection_date,sub_campaign,attachment_count,attachment_names",
        "2026-03-01,Class A,1,1_guide.pdf",
    ]


@pytest.mark.parametrize("name, expected", [
    ("report.pdf", "report.pdf"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\evil.exe", "evil.exe"),
    ('we"ird:na<me>?.csv', "we_ird_na_me__.csv"),
    ("..", "file"),
    ("", "file"),
    ("  .hidden. ", "hidden"),
])
def test_safe_name_cannot_escape_the_folder(name, expected):
    assert pipeline.safe_name(name) == expected


def test_downloaded_files_are_cleaned_up_after_each_row(tmp_path):
    seen = []

    class SpyLLM:
        def generate_csv(self, row_date, sub_campaign, files):
            seen.extend(files)
            return "a\n1\n"

    run(FakeClient([record("r1")]), SpyLLM(), tmp_path)
    assert seen and not any(path.exists() for path in seen)
