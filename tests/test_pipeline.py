from datetime import date

import pytest

import pipeline
from fakes import FakeClient, attachment, record
from lark_base import FIELD_OUTPUT, FIELD_PROCESS, FIELD_STATUS
from llm import StubLLM


def run(client, llm, **kwargs):
    return pipeline.process_ticked_rows(client, llm, **kwargs)


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


def status_of(client, record_id):
    return client.records[record_id]["fields"][FIELD_STATUS]


# -- row model ---------------------------------------------------------------


def test_parse_row_reads_every_column():
    row = pipeline.parse_row(record("r1", attachments=[attachment("tok1", "a.pdf", 10, "application/pdf")],
                                    output=[attachment("out1", "old.csv", 5, "text/csv")]))
    assert row.record_id == "r1"
    assert row.date == date(2026, 3, 1)
    assert row.sub_campaign == "Class A"
    assert row.process is True
    assert row.status == "Not Processed"
    assert row.attachments == (pipeline.Attachment("tok1", "a.pdf", 10, "application/pdf"),)
    assert row.outputs == (pipeline.Attachment("out1", "old.csv", 5, "text/csv"),)


def test_parse_row_tolerates_empty_cells():
    row = pipeline.parse_row(record("r2", ticked=False, status=None, sub_campaign=None, date_ms=None,
                                    attachments=None))
    assert row.date is None
    assert row.sub_campaign == ""
    assert row.status == ""
    assert row.attachments == ()
    assert row.outputs == ()
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
    assert client.uploads == {}


# -- claim, process, finalize ------------------------------------------------


def test_each_row_is_claimed_just_before_it_is_processed():
    client = FakeClient([record("r1"), record("r2", ticked=False), record("r3")])
    report = run(client, RecordingLLM())

    claim = {FIELD_STATUS: "Queued", FIELD_PROCESS: False}
    done = {FIELD_STATUS: "Integrated"}
    assert [(rid, fields.get(FIELD_STATUS)) for rid, fields in client.updates] == [
        ("r1", "Queued"), ("r1", "Integrated"), ("r3", "Queued"), ("r3", "Integrated")]
    assert client.updates[0] == ("r1", claim)
    assert {k: v for k, v in client.updates[1][1].items() if k != FIELD_OUTPUT} == done
    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Integrated"), ("r3", "Integrated")]
    assert report.finished_at is not None
    assert report.remaining == 0
    assert status_of(client, "r2") == "Not Processed"


def test_a_failing_row_is_marked_failed_and_the_others_still_run():
    client = FakeClient([record("r1", sub_campaign="bad"), record("r2", sub_campaign="good")])
    report = run(client, RecordingLLM(fail_for_sub_campaign="bad"))

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Integrated")]
    assert "model exploded" in report.results[0].detail
    assert status_of(client, "r1") == "Failed"
    assert client.records["r1"]["fields"][FIELD_PROCESS] is False
    assert FIELD_OUTPUT not in client.records["r1"]["fields"]
    assert status_of(client, "r2") == "Integrated"


def test_a_row_that_cannot_be_claimed_stays_ticked_and_is_not_retried_in_the_same_run():
    client = FakeClient([record("r1"), record("r2")], fail_updates_for={"r1"})
    llm = RecordingLLM()
    report = run(client, llm)

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Integrated")]
    assert "could not claim" in report.results[0].detail
    assert client.records["r1"]["fields"][FIELD_PROCESS] is True
    assert report.remaining == 1
    assert len(llm.calls) == 1


def test_processed_rows_are_not_picked_up_again():
    client = FakeClient([record("r1")])
    run(client, RecordingLLM())
    assert run(client, RecordingLLM()).results == []


def test_a_row_claimed_by_another_run_meanwhile_is_not_processed_twice():
    client = FakeClient([record("r1"), record("r2")])

    def another_run_claims_r2():
        client.records["r2"]["fields"].update({FIELD_STATUS: "Queued", FIELD_PROCESS: False})

    llm = RecordingLLM(on_call=another_run_claims_r2)
    report = run(client, llm)
    assert [r.record_id for r in report.results] == ["r1"]
    assert len(llm.calls) == 1


def test_a_row_ticked_while_the_run_is_going_is_picked_up():
    client = FakeClient([record("r1"), record("r2", ticked=False)])
    llm = RecordingLLM(on_call=lambda: client.records["r2"]["fields"].update({FIELD_PROCESS: True}))
    report = run(client, llm)
    assert [r.record_id for r in report.results] == ["r1", "r2"]


def test_results_are_yielded_one_row_at_a_time():
    client = FakeClient([record("r1"), record("r2")])
    results = pipeline.run_ticked_rows(client, RecordingLLM())
    assert next(results).record_id == "r1"
    assert status_of(client, "r1") == "Integrated"
    assert status_of(client, "r2") == "Not Processed"  # r2 is not even claimed yet
    assert client.records["r2"]["fields"][FIELD_PROCESS] is True


def test_no_new_row_starts_after_the_time_budget_and_the_rest_stay_ticked():
    clock = [0.0]

    def slow():
        clock[0] += 100

    client = FakeClient([record("r1"), record("r2"), record("r3")])
    results = list(pipeline.run_ticked_rows(client, RecordingLLM(on_call=slow), budget_seconds=150,
                                            clock=lambda: clock[0]))
    assert [r.record_id for r in results] == ["r1", "r2"]  # r2 started at 100s, r3 would start at 200s
    assert client.records["r3"]["fields"][FIELD_PROCESS] is True
    assert status_of(client, "r3") == "Not Processed"


def test_the_report_counts_rows_left_ticked_by_the_budget():
    client = FakeClient([record("r1"), record("r2")])
    report = run(client, RecordingLLM(), budget_seconds=0)
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


def test_cmd_run_prints_a_summary_and_returns_1_when_a_row_failed(capsys):
    client = FakeClient([record("r1", sub_campaign="bad"), record("r2")])
    code = pipeline.cmd_run(client, RecordingLLM(fail_for_sub_campaign="bad", csv_text="h\nv\n"))
    out = capsys.readouterr().out
    assert code == 1
    assert "Processed 2 row(s): 1 Integrated, 1 Failed." in out
    assert "-> 2026-03-01_r2.csv" in out


# -- attachments and CSV output ----------------------------------------------


def test_llm_receives_the_rows_inputs_and_downloaded_files():
    client = FakeClient([record("r1", sub_campaign="Class B", attachments=[
        attachment("tokA", "one.pdf"), attachment("tokB", "two.docx")])])
    llm = RecordingLLM()
    run(client, llm)

    assert client.downloads == ["tokA", "tokB"]
    assert llm.calls == [(date(2026, 3, 1), "Class B",
                          [("1_one.pdf", b"bytes of tokA"), ("2_two.docx", b"bytes of tokB")])]


def test_csv_is_uploaded_as_date_and_record_id_and_attached_to_the_row():
    client = FakeClient([record("rec123")])
    report = run(client, RecordingLLM(csv_text="x,y\n1,2\n"))

    assert client.uploads == {"up1": ("2026-03-01_rec123.csv", b"x,y\n1,2\n")}
    assert report.results[0].file_name == "2026-03-01_rec123.csv"
    assert client.records["rec123"]["fields"][FIELD_OUTPUT] == [{"file_token": "up1"}]
    assert status_of(client, "rec123") == "Integrated"


def test_csv_text_is_uploaded_as_utf8():
    client = FakeClient([record("r1")])
    run(client, RecordingLLM(csv_text="name\nJosé 李\n"))
    assert client.uploads["up1"][1] == "name\nJosé 李\n".encode("utf-8")


def test_a_row_without_a_date_gets_a_no_date_file_name():
    client = FakeClient([record("r1", date_ms=None)])
    assert run(client, RecordingLLM()).results[0].file_name == "no-date_r1.csv"


def test_rerunning_a_row_replaces_its_csv():
    client = FakeClient([record("rec1", output=[attachment("old", "old.csv", 9, "text/csv")])])
    run(client, RecordingLLM(csv_text="v\n2\n"))
    assert client.records["rec1"]["fields"][FIELD_OUTPUT] == [{"file_token": "up1"}]


def test_row_without_attachment_fails_without_calling_the_llm_or_uploading():
    client = FakeClient([record("r1", attachments=None)])
    llm = RecordingLLM()
    report = run(client, llm)

    assert report.results[0].status == "Failed"
    assert "no attachment" in report.results[0].detail
    assert llm.calls == []
    assert client.uploads == {}
    assert status_of(client, "r1") == "Failed"


def test_download_error_marks_the_row_failed_and_uploads_nothing_for_it():
    client = FakeClient([record("r1"), record("r2")], fail_downloads_for={"tok-r1"})
    report = run(client, RecordingLLM())

    assert [(r.record_id, r.status) for r in report.results] == [("r1", "Failed"), ("r2", "Integrated")]
    assert "download refused" in report.results[0].detail
    assert [name for name, _ in client.uploads.values()] == ["2026-03-01_r2.csv"]


def test_upload_error_marks_the_row_failed_and_does_not_integrate_it():
    client = FakeClient([record("r1")], fail_uploads=True)
    report = run(client, RecordingLLM())

    assert report.results[0].status == "Failed"
    assert "upload refused" in report.results[0].detail
    assert status_of(client, "r1") == "Failed"
    assert FIELD_OUTPUT not in client.records["r1"]["fields"]


def test_empty_csv_from_the_llm_fails_the_row():
    client = FakeClient([record("r1")])
    report = run(client, RecordingLLM(csv_text="  \n"))
    assert report.results[0].status == "Failed"
    assert "empty CSV" in report.results[0].detail
    assert client.uploads == {}


def test_stub_llm_output_is_a_valid_csv_describing_its_inputs():
    client = FakeClient([record("r1", attachments=[attachment("t", "guide.pdf")])])
    run(client, StubLLM())
    text = client.uploads["up1"][1].decode("utf-8")
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


# -- multi-disposition chaining, merging, and CSV slicing --------------------


def test_slice_csv_by_rule_name_splits_each_disposition_with_header():
    row = pipeline.parse_row(record("r1"))
    csv_text = (
        "Rule Name,Placement Name,Intent\n"
        "TALA_EDC_RIGHT_PARTY_CONTACT,default_placement,opening_greeting_intent\n"
        "TALA_EDC_PTP,default_placement,ptp_payment_amount_confirmation_intent\n"
        "TALA_EDC_PTP,default_placement,summarize_amount_intent\n"
    )
    slices = pipeline.slice_csv_by_rule_name(row, csv_text)
    assert [s.rule_name for s in slices] == ["TALA_EDC_RIGHT_PARTY_CONTACT", "TALA_EDC_PTP"]
    assert slices[0].file_name == "2026-03-01_r1_TALA_EDC_RIGHT_PARTY_CONTACT.csv"
    assert slices[0].csv_text.splitlines() == [
        "Rule Name,Placement Name,Intent",
        "TALA_EDC_RIGHT_PARTY_CONTACT,default_placement,opening_greeting_intent",
    ]
    assert len(slices[1].csv_text.splitlines()) == 3


def test_merge_csvs_rejects_mismatched_headers():
    with pytest.raises(ValueError, match="headers do not match"):
        pipeline.merge_csvs(["a,b\n1,2\n", "a,c\n3,4\n"])


def test_chained_execution_runs_call2_on_call1_csv_without_files():
    import prompts

    calls = []

    class PromptAwareLLM:
        def generate_csv(self, row_date, sub_campaign, files, *, prompt_text=None):
            calls.append(([f.name for f in files], prompt_text))
            if len(calls) == 1:
                return (
                    "Rule Name,Placement Name\n"
                    "CLASS_A_RIGHT_PARTY_CONTACT,default_placement\n"
                    "CLASS_A_PTP,default_placement\n"
                )
            return (
                "Rule Name,Placement Name\n"
                "CLASS_A_PTP_FOLLOW_UP,default_placement\n"
                "CLASS_A_CLAIMING_PAID,default_placement\n"
                "CLASS_A_UNRELATED,default_placement\n"
            )

    selected = prompts.resolve_prompts(["rpc_ptp", "follow_up", "claiming_paid", "unrelated"])
    client = FakeClient([record("r1")])
    report = pipeline.process_ticked_rows(client, PromptAwareLLM(), selected_prompts=selected)

    assert len(calls) == 2
    assert calls[0][0] == ["1_r1.pdf"]
    assert "CLASS_A_PTP" not in calls[0][1]
    assert calls[1][0] == []  # Call 2 does not re-upload attachments
    assert "CLASS_A_PTP,default_placement" in calls[1][1]  # Call 2 is anchored to Call 1 CSV
    result = report.results[0]
    assert result.status == "Integrated"
    assert "chained 2 calls" in result.detail
    assert [s.rule_name for s in result.slices] == [
        "CLASS_A_RIGHT_PARTY_CONTACT",
        "CLASS_A_PTP",
        "CLASS_A_PTP_FOLLOW_UP",
        "CLASS_A_CLAIMING_PAID",
        "CLASS_A_UNRELATED",
    ]
    uploaded_names = [name for name, _ in client.uploads.values()]
    assert uploaded_names == [
        "2026-03-01_r1_CLASS_A_RIGHT_PARTY_CONTACT.csv",
        "2026-03-01_r1_CLASS_A_PTP.csv",
        "2026-03-01_r1_CLASS_A_PTP_FOLLOW_UP.csv",
        "2026-03-01_r1_CLASS_A_CLAIMING_PAID.csv",
        "2026-03-01_r1_CLASS_A_UNRELATED.csv",
    ]
    assert client.records["r1"]["fields"][FIELD_OUTPUT] == [
        {"file_token": f"up{i}"} for i in range(1, 6)
    ]


def test_cmd_run_writes_combined_and_sliced_csvs(tmp_path):
    csv_text = (
        "Rule Name,Placement Name\n"
        "A_RIGHT_PARTY_CONTACT,default_placement\n"
        "A_PTP,default_placement\n"
    )
    code = pipeline.cmd_run(FakeClient([record("r1")]), RecordingLLM(csv_text=csv_text), output_dir=tmp_path)
    assert code == 0
    assert (tmp_path / "2026-03-01_r1.csv").is_file()
    assert (tmp_path / "2026-03-01_r1_A_RIGHT_PARTY_CONTACT.csv").is_file()
    assert (tmp_path / "2026-03-01_r1_A_PTP.csv").is_file()


def test_cli_list_prompts_prints_all_blocks(capsys):
    assert pipeline.main(["--list-prompts"]) == 0
    out = capsys.readouterr().out
    for pid in ("rpc_ptp", "follow_up", "reminder", "claiming_paid", "third_party_contact", "unrelated"):
        assert pid in out
