from datetime import date

import pytest

import pipeline
from fakes import FakeClient, attachment, record
from llm import StubLLM


def run(client, llm, **kwargs):
    return pipeline.process_rows(client, llm, **kwargs)


class RecordingLLM:
    def __init__(self, fail_for_sub_campaign: str | None = None, csv_text: str = "a,b\n1,2\n", on_call=None):
        self.calls = []
        self.fail_for = fail_for_sub_campaign
        self.csv_text = csv_text
        self.on_call = on_call

    def generate_csv(self, row_date, sub_campaign, files):
        # The files only exist during the call, so capture their contents now.
        self.calls.append((row_date, sub_campaign, [(f.name, f.read_bytes()) for f in files]))
        if self.on_call:
            self.on_call()
        if sub_campaign == self.fail_for:
            raise ValueError("model exploded")
        return self.csv_text


# -- row model ---------------------------------------------------------------


def test_parse_row_reads_input_columns():
    row = pipeline.parse_row(record("r1", attachments=[attachment("tok1", "a.pdf", 10, "application/pdf")]))
    assert row.record_id == "r1"
    assert row.date == date(2026, 3, 1)
    assert row.sub_campaign == "Class A"
    assert row.attachments == (pipeline.Attachment("tok1", "a.pdf", 10, "application/pdf"),)


def test_parse_row_tolerates_empty_cells():
    row = pipeline.parse_row(record("r2", sub_campaign=None, date_ms=None, attachments=None))
    assert row.date is None
    assert row.sub_campaign == ""
    assert row.attachments == ()


def test_parse_attachments_skips_items_without_a_file_token():
    parsed = pipeline.parse_attachments([{"name": "x"}, attachment("ok"), "junk"])
    assert [a.file_token for a in parsed] == ["ok"]


def test_dry_run_prints_all_rows_and_downloads_nothing(capsys):
    client = FakeClient([
        record("r1", attachments=[attachment("tok1", "a.pdf", 10)]),
        record("r2", attachments=None),
    ])
    assert pipeline.cmd_dry_run(client) == 0
    out = capsys.readouterr().out
    assert "2 row(s) in the grid." in out
    assert "r1" in out and "a.pdf (10 bytes)" in out
    assert "r2" in out and "no attachment" in out
    assert client.downloads == []


# -- selection, processing, budget -------------------------------------------


def test_selected_rows_are_processed_in_selection_order_and_deduplicated():
    client = FakeClient([record("r1"), record("r2"), record("r3")])
    llm = RecordingLLM()
    report = run(client, llm, record_ids=["r3", "r1", "r3"])

    assert [(r.record_id, r.status) for r in report.results] == [("r3", "Done"), ("r1", "Done")]
    assert report.finished_at is not None
    assert report.remaining == 0
    assert len(llm.calls) == 2


def test_unknown_record_id_fails_without_stopping_other_rows():
    client = FakeClient([record("r1")])
    report = run(client, RecordingLLM(), record_ids=["missing", "r1"])

    assert [(r.record_id, r.status) for r in report.results] == [("missing", "Failed"), ("r1", "Done")]
    assert "not found" in report.results[0].detail


def test_a_failing_row_does_not_stop_the_others():
    client = FakeClient([record("r1", sub_campaign="bad"), record("r2", sub_campaign="good")])
    report = run(client, RecordingLLM(fail_for_sub_campaign="bad"))

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Done")]
    assert "model exploded" in report.results[0].detail
    assert report.results[0].csv_text is None
    assert report.results[1].csv_text == "a,b\n1,2\n"


def test_results_are_yielded_one_row_at_a_time():
    client = FakeClient([record("r1"), record("r2")])
    results = pipeline.run_rows(client, RecordingLLM())
    assert next(results).record_id == "r1"
    assert client.downloads == ["tok-r1"]


def test_no_new_row_starts_after_the_time_budget():
    clock = [0.0]

    def slow():
        clock[0] += 100

    client = FakeClient([record("r1"), record("r2"), record("r3")])
    results = list(pipeline.run_rows(client, RecordingLLM(on_call=slow), budget_seconds=150,
                                     clock=lambda: clock[0]))
    assert [r.record_id for r in results] == ["r1", "r2"]  # r2 started at 100s, r3 would start at 200s


def test_the_report_counts_rows_not_reached_by_the_budget():
    client = FakeClient([record("r1"), record("r2")])
    report = run(client, RecordingLLM(), record_ids=["r1", "r2"], budget_seconds=0)
    assert report.results == []
    assert report.remaining == 2


def test_a_passed_in_report_is_filled_in_place():
    report = pipeline.RunReport()
    returned = run(FakeClient([record("r1")]), RecordingLLM(), report=report)
    assert returned is report
    assert len(report.results) == 1


def test_a_run_that_cannot_read_the_grid_still_sets_finished_at():
    class BrokenClient(FakeClient):
        def list_records(self, **_):
            raise RuntimeError("network down")

    report = pipeline.RunReport()
    with pytest.raises(RuntimeError):
        run(BrokenClient([]), StubLLM(), report=report)
    assert report.finished_at is not None


def test_cmd_run_writes_csvs_to_output_dir_and_returns_1_when_a_row_failed(capsys, tmp_path):
    client = FakeClient([record("r1", sub_campaign="bad"), record("r2")])
    code = pipeline.cmd_run(client, RecordingLLM(fail_for_sub_campaign="bad", csv_text="h\nv\n"),
                            output_dir=tmp_path)
    out = capsys.readouterr().out
    assert code == 1
    assert "Processed 2 row(s): 1 Done, 1 Failed." in out
    saved = tmp_path / "2026-03-01_r2.csv"
    assert saved.read_text(encoding="utf-8") == "h\nv\n"
    assert str(saved) in out


def test_cli_run_requires_row_or_all(capsys):
    with pytest.raises(SystemExit) as exc:
        pipeline.main(["--run"])
    assert exc.value.code == 2
    assert "--row" in capsys.readouterr().err


# -- attachments and CSV output ----------------------------------------------


def test_llm_receives_the_rows_inputs_and_downloaded_files():
    client = FakeClient([record("r1", sub_campaign="Class B", attachments=[
        attachment("tokA", "one.pdf"), attachment("tokB", "two.docx")])])
    llm = RecordingLLM()
    run(client, llm)

    assert client.downloads == ["tokA", "tokB"]
    assert llm.calls == [(date(2026, 3, 1), "Class B",
                          [("1_one.pdf", b"bytes of tokA"), ("2_two.docx", b"bytes of tokB")])]


def test_csv_result_carries_filename_and_utf8_text():
    client = FakeClient([record("rec123")])
    report = run(client, RecordingLLM(csv_text="name\nJosé 李\n"))

    assert report.results[0].file_name == "2026-03-01_rec123.csv"
    assert report.results[0].csv_text == "name\nJosé 李\n"
    assert report.results[0].status == "Done"


def test_a_row_without_a_date_gets_a_no_date_file_name():
    client = FakeClient([record("r1", date_ms=None)])
    assert run(client, RecordingLLM()).results[0].file_name == "no-date_r1.csv"


def test_row_without_attachment_fails_without_calling_the_llm():
    client = FakeClient([record("r1", attachments=None)])
    llm = RecordingLLM()
    report = run(client, llm)

    assert report.results[0].status == "Failed"
    assert "no attachment" in report.results[0].detail
    assert llm.calls == []


def test_download_error_marks_the_row_failed():
    client = FakeClient([record("r1"), record("r2")], fail_downloads_for={"tok-r1"})
    report = run(client, RecordingLLM())

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Done")]
    assert "download refused" in report.results[0].detail


def test_empty_csv_from_the_llm_fails_the_row():
    client = FakeClient([record("r1")])
    report = run(client, RecordingLLM(csv_text="  \n"))
    assert report.results[0].status == "Failed"
    assert "empty CSV" in report.results[0].detail


def test_stub_llm_output_is_a_valid_csv_describing_its_inputs():
    client = FakeClient([record("r1", attachments=[attachment("t", "guide.pdf")])])
    report = run(client, StubLLM())
    text = report.results[0].csv_text
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


def test_downloaded_files_are_cleaned_up_after_each_row():
    seen = []

    class SpyLLM:
        def generate_csv(self, row_date, sub_campaign, files):
            seen.extend(files)
            return "a\n1\n"

    run(FakeClient([record("r1")]), SpyLLM())
    assert seen and not any(path.exists() for path in seen)
