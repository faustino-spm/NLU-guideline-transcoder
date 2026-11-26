"""NanoGPTLLM against a mocked NanoGPT API (no network)."""

from __future__ import annotations

import base64
import json
from datetime import date

import httpx
import pytest

import llm
from lark_base import ConfigError
from llm import LLMError, NANOGPT_MODEL, NANOGPT_URL, NanoGPTLLM, clean_csv, validate_csv

GOOD = "name,score\nAnn,5\nBob,7\n"


def reply(text: str, finish: str = "stop", reasoning: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {"choices": [{"message": message, "finish_reason": finish}]}


class FakeNanoGPT:
    """Plays back a list of (status, json) responses and records the requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload)

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def make(tmp_path, *responses, prompt="Parse it.", **kwargs):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    fake = FakeNanoGPT(*responses)
    sleeps: list[float] = []
    client = NanoGPTLLM("test-key", NANOGPT_MODEL, prompt_file, transport=httpx.MockTransport(fake),
                        sleep=sleeps.append, **kwargs)
    return client, fake, sleeps


def pdf(tmp_path, name="a.pdf", data=b"%PDF-1.7 fake"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# -- the request -------------------------------------------------------------


def test_request_carries_key_model_files_and_prompt(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD)), prompt="  Parse the attachment.  ")
    file = pdf(tmp_path, data=b"hello")

    result = client.generate_csv(date(2026, 3, 1), "Class A", [file])

    assert result == GOOD
    request = fake.requests[0]
    assert str(request.url) == NANOGPT_URL
    assert request.headers["authorization"] == "Bearer test-key"
    assert "test-key" not in str(request.url)
    body = fake.bodies[0]
    assert body["model"] == "google/gemini-4-hydrogen"
    assert body["reasoning_effort"] == "high"
    assert body["messages"][0]["role"] == "system"
    assert "CSV only" in body["messages"][0]["content"]
    parts = body["messages"][1]["content"]
    assert parts[0] == {
        "type": "file",
        "file": {
            "filename": "a.pdf",
            "file_data": f"data:application/pdf;base64,{base64.b64encode(b'hello').decode()}",
        },
    }
    assert parts[-1] == {"type": "text", "text": "Parse the attachment."}  # the row's values are not added unasked


def test_placeholders_in_the_prompt_are_replaced_with_the_rows_values(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD)), (200, reply(GOOD)),
                           prompt='Parse. Add {date} and {sub_campaign}. Keep {"json": "braces"}.')
    client.generate_csv(date(2026, 3, 1), "Class A", [pdf(tmp_path)])
    client.generate_csv(None, "", [pdf(tmp_path)])
    texts = [b["messages"][1]["content"][-1]["text"] for b in fake.bodies]
    assert texts[0] == 'Parse. Add 2026-03-01 and Class A. Keep {"json": "braces"}.'
    assert texts[1] == 'Parse. Add unknown and unknown. Keep {"json": "braces"}.'


def test_every_file_is_sent_in_order_by_type(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD)))
    txt = tmp_path / "0_note.txt"
    txt.write_text("hello", encoding="utf-8")
    files = [txt, pdf(tmp_path, "1_a.xlsx"), pdf(tmp_path, "2_b.DOCX"), pdf(tmp_path, "3_c.png")]
    client.generate_csv(None, "", files)
    parts = fake.bodies[0]["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "Attachment (0_note.txt):\nhello"}
    assert parts[1]["type"] == "file"
    assert parts[1]["file"]["file_data"].startswith(
        "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,"
    )
    assert parts[2]["type"] == "file"
    assert parts[2]["file"]["file_data"].startswith(
        "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,"
    )
    assert parts[3]["type"] == "image_url"
    assert parts[3]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[4] == {"type": "text", "text": "Parse it."}


def test_unsupported_file_type_fails_before_any_request(tmp_path):
    client, fake, _ = make(tmp_path)
    with pytest.raises(LLMError, match=r"unsupported attachment type: x\.exe"):
        client.generate_csv(None, "", [pdf(tmp_path, "x.exe")])
    assert fake.requests == []


def test_prompt_file_is_reread_on_every_call(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD)), (200, reply(GOOD)), prompt="first")
    client.generate_csv(None, "", [pdf(tmp_path)])
    client.prompt_file.write_text("second", encoding="utf-8")
    client.generate_csv(None, "", [pdf(tmp_path)])
    assert fake.bodies[0]["messages"][1]["content"][-1]["text"].endswith("first")
    assert fake.bodies[1]["messages"][1]["content"][-1]["text"].endswith("second")


def test_empty_prompt_file_fails_before_any_request(tmp_path):
    client, fake, _ = make(tmp_path, prompt="  \n")
    with pytest.raises(LLMError, match="prompt.txt is empty"):
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert fake.requests == []


# -- the reply ---------------------------------------------------------------


def test_code_fence_around_the_csv_is_removed(tmp_path):
    client, _, _ = make(tmp_path, (200, reply("```csv\n" + GOOD + "```")))
    assert client.generate_csv(None, "", [pdf(tmp_path)]) == GOOD


def test_reasoning_field_is_ignored(tmp_path):
    client, _, _ = make(tmp_path, (200, reply(GOOD, reasoning="let me think")))
    assert client.generate_csv(None, "", [pdf(tmp_path)]) == GOOD


def test_invalid_csv_is_asked_for_once_more(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply("Sure! Here you go.")), (200, reply(GOOD)))
    assert client.generate_csv(None, "", [pdf(tmp_path)]) == GOOD
    assert len(fake.requests) == 2


def test_two_unusable_csvs_fail_the_row(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply("a,b\n1,2,3\n")), (200, reply("a,b\n")))
    with pytest.raises(LLMError, match="no usable CSV after 2 attempts: the CSV has no data rows"):
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert len(fake.requests) == 2


def test_answer_cut_off_at_the_output_limit_is_an_error_not_a_partial_csv(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD, finish="length")))
    with pytest.raises(LLMError, match="cut off"):
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert len(fake.requests) == 1


def test_empty_choices_is_an_error(tmp_path):
    client, _, _ = make(tmp_path, (200, {"choices": []}))
    with pytest.raises(LLMError, match="no choices"):
        client.generate_csv(None, "", [pdf(tmp_path)])


# -- errors and retries ------------------------------------------------------


def test_rate_limit_is_retried_after_a_pause(tmp_path):
    client, fake, sleeps = make(tmp_path, (429, {"error": {"message": "quota"}}), (503, {}), (200, reply(GOOD)))
    assert client.generate_csv(None, "", [pdf(tmp_path)]) == GOOD
    assert sleeps == [5.0, 20.0]
    assert len(fake.requests) == 3


def test_rate_limit_that_never_clears_reports_nanogpt_message(tmp_path):
    error = (429, {"error": {"message": "You exceeded your current quota"}})
    client, fake, sleeps = make(tmp_path, error, error, error)
    with pytest.raises(LLMError, match="429: You exceeded your current quota"):
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert len(fake.requests) == 3
    assert len(sleeps) == 2


def test_bad_request_is_not_retried(tmp_path):
    client, fake, sleeps = make(tmp_path, (400, {"error": {"message": "API key not valid"}}))
    with pytest.raises(LLMError, match="400: API key not valid"):
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert len(fake.requests) == 1
    assert sleeps == []


def test_network_failure_is_an_llm_error_that_does_not_leak_the_key(tmp_path):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("p", encoding="utf-8")
    client = NanoGPTLLM("secret-key", prompt_file=prompt_file, transport=httpx.MockTransport(boom))
    with pytest.raises(LLMError, match="could not reach NanoGPT: ConnectError") as info:
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert "secret-key" not in str(info.value)


# -- CSV helpers -------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("a,b\n1,2", "a,b\n1,2\n"),
    ("\ufeffa,b\r\n1,2\r\n", "a,b\r\n1,2\n"),
    ("```\na,b\n1,2\n```", "a,b\n1,2\n"),
    ("  \n ", ""),
])
def test_clean_csv(raw, expected):
    assert clean_csv(raw) == expected


def test_validate_csv_accepts_quoted_commas_and_newlines_and_blank_lines():
    validate_csv('name,note\n"Smith, J","two\nlines"\n\nAnn,ok\n')


@pytest.mark.parametrize("text, message", [
    ("", "no text"),
    ("only,a,header\n", "no data rows"),
    ("a,b\n1\n", "row 2 has 1 columns, the header has 2"),
    ('a,b\n"unterminated,2\n', "not valid CSV"),
])
def test_validate_csv_rejects(text, message):
    with pytest.raises(LLMError, match=message):
        validate_csv(text)


# -- choosing the LLM --------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    for name in ("LLM", "NANOGPT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_llm_from_env_defaults_to_nanogpt_with_hydrogen_high(env):
    env.setenv("NANOGPT_API_KEY", " key ")
    chosen = llm.llm_from_env()
    assert isinstance(chosen, NanoGPTLLM)
    assert chosen.model == "google/gemini-4-hydrogen"
    assert chosen._http.headers["authorization"] == "Bearer key"


def test_llm_from_env_stub_needs_no_key(env):
    env.setenv("LLM", "Stub")
    assert isinstance(llm.llm_from_env(), llm.StubLLM)


def test_llm_from_env_without_a_key_is_a_config_error_not_a_silent_stub(env):
    with pytest.raises(ConfigError, match="NANOGPT_API_KEY"):
        llm.llm_from_env()


def test_llm_from_env_rejects_an_unknown_choice(env):
    env.setenv("LLM", "openai")
    with pytest.raises(ConfigError, match="'nanogpt' or 'stub'"):
        llm.llm_from_env()


def test_llm_from_env_needs_the_prompt_file(env, tmp_path):
    env.setenv("NANOGPT_API_KEY", "key")
    env.setattr(llm, "DEFAULT_PROMPT_FILE", tmp_path / "missing.txt")
    with pytest.raises(ConfigError, match="missing.txt is missing"):
        llm.llm_from_env()
