"""LLM clients (OpenAI-compatible / NanoGPT and Google Gemini) against mocked APIs (no network)."""

from __future__ import annotations

import base64
import json
import zipfile
from datetime import date

import httpx
import pytest

import llm
from lark_base import ConfigError
from llm import (
    GEMINI_MODEL,
    GEMINI_URL,
    GeminiLLM,
    LLMError,
    NANOGPT_MODEL,
    NANOGPT_URL,
    NanoGPTLLM,
    OPENAI_MODEL,
    OPENAI_URL,
    OpenAICompatibleLLM,
    clean_csv,
    validate_csv,
)

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


def make_docx(tmp_path, name="doc.docx", text="Guideline row", header="", image_bytes: bytes | None = None):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
        if header:
            zf.writestr(
                "word/header1.xml",
                f'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>",
            )
        if image_bytes is not None:
            zf.writestr("word/media/image1.png", image_bytes)
    return path


def make_xlsx(tmp_path, name="sheet.xlsx"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Agent</t></si><si><t>Note</t></si><si><t>Date</t></si></sst>",
        )
        zf.writestr(
            "xl/styles.xml",
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',
        )
        zf.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Calibration" sheetId="1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
            # Blank column B (sparse): C2 should stay in column 3, and s="1" formats Excel serial 46082 -> 2026-03-01
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Ann</t></is></c><c r="C2" s="1"><v>46082</v></c></row>'
            "</sheetData></worksheet>",
        )
    return path


def test_every_file_is_sent_in_order_by_type(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD)))
    txt = tmp_path / "0_note.txt"
    txt.write_text("hello", encoding="utf-8")
    files = [
        txt,
        make_xlsx(tmp_path, "1_a.xlsx"),
        make_docx(tmp_path, "2_b.DOCX", header="QA v2", image_bytes=b"\x89PNGfake"),
        pdf(tmp_path, "3_c.png"),
        pdf(tmp_path, "4_d.pdf"),
    ]
    client.generate_csv(None, "", files)
    parts = fake.bodies[0]["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "Attachment (0_note.txt):\nhello"}
    assert parts[1] == {
        "type": "text",
        "text": "Attachment (1_a.xlsx):\n=== Sheet: Calibration ===\nAgent,Note,Date\nAnn,,2026-03-01",
    }
    assert parts[2] == {
        "type": "text",
        "text": "Attachment (2_b.DOCX):\n=== Header ===\nQA v2\n\nGuideline row",
    }
    assert parts[3] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{base64.b64encode(b'\x89PNGfake').decode()}"},
    }
    assert parts[4]["type"] == "image_url"
    assert parts[4]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[5]["type"] == "file"
    assert parts[5]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert parts[6] == {"type": "text", "text": "Parse it."}


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


def test_prompt_text_override_works_without_files_for_chained_call(tmp_path):
    client, fake, _ = make(tmp_path, (200, reply(GOOD)), prompt="ignored")
    result = client.generate_csv(date(2026, 3, 1), "Class A", [], prompt_text="  Stage 2 prompt  ")
    assert result == GOOD
    parts = fake.bodies[0]["messages"][1]["content"]
    assert parts == [{"type": "text", "text": "Stage 2 prompt"}]


def test_empty_prompt_text_override_fails_before_any_request(tmp_path):
    client, fake, _ = make(tmp_path, prompt="valid")
    with pytest.raises(LLMError, match="prompt is empty"):
        client.generate_csv(None, "", [], prompt_text="   \n")
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


# -- generic OpenAI-compatible endpoint --------------------------------------


def test_openai_compatible_endpoint_normalizes_base_url_and_omits_unset_reasoning_effort(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Parse it.", encoding="utf-8")
    fake = FakeNanoGPT((200, reply(GOOD)))
    client = OpenAICompatibleLLM(
        "sk-test",
        model="gpt-5-mini",
        prompt_file=prompt_file,
        url="https://openrouter.ai/api/v1/",
        reasoning_effort=None,
        transport=httpx.MockTransport(fake),
    )

    assert client.generate_csv(date(2026, 3, 1), "Class A", [pdf(tmp_path)]) == GOOD
    assert str(fake.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert fake.requests[0].headers["authorization"] == "Bearer sk-test"
    assert fake.bodies[0]["model"] == "gpt-5-mini"
    assert "reasoning_effort" not in fake.bodies[0]


# -- Google Gemini native API ------------------------------------------------


def gemini_reply(text: str, finish: str = "STOP", thought: str | None = None) -> dict:
    parts: list[dict] = []
    if thought is not None:
        parts.append({"text": thought, "thought": True})
    parts.append({"text": text})
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}


def make_gemini(tmp_path, *responses, prompt="Parse it.", model=GEMINI_MODEL, **kwargs):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    fake = FakeNanoGPT(*responses)
    sleeps: list[float] = []
    client = GeminiLLM(
        "gem-test-key",
        model,
        prompt_file,
        transport=httpx.MockTransport(fake),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, fake, sleeps


def test_gemini_request_carries_key_header_model_files_and_prompt(tmp_path):
    client, fake, _ = make_gemini(tmp_path, (200, gemini_reply(GOOD)), prompt="  Parse {sub_campaign} ({date}).  ")
    txt = tmp_path / "0_note.txt"
    txt.write_text("hello", encoding="utf-8")
    files = [
        txt,
        make_xlsx(tmp_path, "1_a.xlsx"),
        make_docx(tmp_path, "2_b.DOCX", header="QA v2", image_bytes=b"\x89PNGfake"),
        pdf(tmp_path, "3_c.png", data=b"\x89PNGraw"),
        pdf(tmp_path, "4_d.pdf", data=b"hello"),
    ]

    result = client.generate_csv(date(2026, 3, 1), "Class A", files)

    assert result == GOOD
    request = fake.requests[0]
    assert str(request.url) == GEMINI_URL.format(model=GEMINI_MODEL)
    assert request.headers["x-goog-api-key"] == "gem-test-key"
    assert "gem-test-key" not in str(request.url)
    body = fake.bodies[0]
    assert body["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "high"}}
    assert "CSV only" in body["system_instruction"]["parts"][0]["text"]
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "Attachment (0_note.txt):\nhello"}
    assert parts[1] == {
        "text": "Attachment (1_a.xlsx):\n=== Sheet: Calibration ===\nAgent,Note,Date\nAnn,,2026-03-01",
    }
    assert parts[2] == {"text": "Attachment (2_b.DOCX):\n=== Header ===\nQA v2\n\nGuideline row"}
    assert parts[3] == {
        "inline_data": {"mime_type": "image/png", "data": base64.b64encode(b"\x89PNGfake").decode()},
    }
    assert parts[4] == {
        "inline_data": {"mime_type": "image/png", "data": base64.b64encode(b"\x89PNGraw").decode()},
    }
    assert parts[5] == {
        "inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(b"hello").decode()},
    }
    assert parts[6] == {"text": "Parse Class A (2026-03-01)."}


def test_gemini_prompt_text_override_and_thought_parts_ignored(tmp_path):
    client, fake, _ = make_gemini(tmp_path, (200, gemini_reply(GOOD, thought="internal reasoning")))
    result = client.generate_csv(date(2026, 3, 1), "Class A", [], prompt_text="  Stage 2 prompt  ")
    assert result == GOOD
    assert fake.bodies[0]["contents"][0]["parts"] == [{"text": "Stage 2 prompt"}]


def test_gemini_errors_and_retries(tmp_path):
    client, fake, sleeps = make_gemini(
        tmp_path,
        (429, {"error": {"message": "quota"}}),
        (503, {}),
        (200, gemini_reply(GOOD)),
    )
    assert client.generate_csv(None, "", [pdf(tmp_path)]) == GOOD
    assert sleeps == [5.0, 20.0]
    assert len(fake.requests) == 3

    client_max, _, _ = make_gemini(tmp_path, (200, gemini_reply(GOOD, finish="MAX_TOKENS")))
    with pytest.raises(LLMError, match="cut off"):
        client_max.generate_csv(None, "", [pdf(tmp_path)])

    client_blocked, _, _ = make_gemini(tmp_path, (200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(LLMError, match="SAFETY"):
        client_blocked.generate_csv(None, "", [pdf(tmp_path)])


def test_gemini_network_failure_does_not_leak_key(tmp_path):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("p", encoding="utf-8")
    client = GeminiLLM("gem-secret", prompt_file=prompt_file, transport=httpx.MockTransport(boom))
    with pytest.raises(LLMError, match="could not reach Gemini: ConnectError") as info:
        client.generate_csv(None, "", [pdf(tmp_path)])
    assert "gem-secret" not in str(info.value)


# -- choosing the LLM --------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(llm, "LLM_PROVIDER", "nanogpt")
    for name in (
        "LLM",
        "NANOGPT_API_KEY",
        "NANOGPT_MODEL",
        "NANOGPT_URL",
        "NANOGPT_REASONING_EFFORT",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_THINKING_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_llm_from_env_defaults_to_nanogpt_with_hydrogen_high(env):
    env.setenv("NANOGPT_API_KEY", " key ")
    chosen = llm.llm_from_env()
    assert isinstance(chosen, NanoGPTLLM)
    assert chosen.model == "google/gemini-4-hydrogen"
    assert chosen.url == NANOGPT_URL
    assert chosen.reasoning_effort == "high"
    assert chosen._http.headers["authorization"] == "Bearer key"


def test_switching_backend_llm_provider_constant_selects_gemini(env):
    env.setattr(llm, "LLM_PROVIDER", "gemini")
    env.setenv("GEMINI_API_KEY", " gem-key ")
    chosen = llm.llm_from_env()
    assert isinstance(chosen, GeminiLLM)
    assert chosen.model == GEMINI_MODEL
    assert chosen._http.headers["x-goog-api-key"] == "gem-key"


def test_switching_backend_llm_provider_constant_selects_openai(env):
    env.setattr(llm, "LLM_PROVIDER", "openai")
    env.setenv("OPENAI_API_KEY", " sk-key ")
    env.setenv("OPENAI_BASE_URL", "https://api.example.com/v1")
    env.setenv("OPENAI_MODEL", "gpt-5-mini")
    env.setenv("OPENAI_REASONING_EFFORT", "medium")
    chosen = llm.llm_from_env()
    assert isinstance(chosen, OpenAICompatibleLLM)
    assert not isinstance(chosen, NanoGPTLLM)
    assert chosen.model == "gpt-5-mini"
    assert chosen.url == "https://api.example.com/v1/chat/completions"
    assert chosen.reasoning_effort == "medium"
    assert chosen._http.headers["authorization"] == "Bearer sk-key"


def test_llm_from_env_openai_defaults(env):
    env.setenv("LLM", "openai")
    env.setenv("OPENAI_API_KEY", "sk-key")
    chosen = llm.llm_from_env()
    assert isinstance(chosen, OpenAICompatibleLLM)
    assert chosen.model == OPENAI_MODEL
    assert chosen.url == OPENAI_URL
    assert chosen.reasoning_effort is None


def test_llm_from_env_gemini_supports_model_and_thinking_level_override(env):
    env.setenv("LLM", "gemini")
    env.setenv("GEMINI_API_KEY", "gem-key")
    env.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    env.setenv("GEMINI_THINKING_LEVEL", "medium")
    chosen = llm.llm_from_env()
    assert isinstance(chosen, GeminiLLM)
    assert chosen.model == "gemini-2.5-pro"
    assert chosen.thinking_level == "medium"

    env.setenv("GEMINI_THINKING_LEVEL", "")
    chosen_none = llm.llm_from_env()
    assert isinstance(chosen_none, GeminiLLM)
    assert chosen_none.thinking_level is None


def test_llm_from_env_stub_needs_no_key(env):
    env.setenv("LLM", "Stub")
    assert isinstance(llm.llm_from_env(), llm.StubLLM)


def test_llm_from_env_without_a_key_is_a_config_error_not_a_silent_stub(env):
    with pytest.raises(ConfigError, match="NANOGPT_API_KEY"):
        llm.llm_from_env()

    env.setattr(llm, "LLM_PROVIDER", "gemini")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        llm.llm_from_env()

    env.setattr(llm, "LLM_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        llm.llm_from_env()


def test_llm_from_env_rejects_an_unknown_choice(env):
    env.setenv("LLM", "anthropic")
    with pytest.raises(ConfigError, match="'nanogpt', 'openai', 'gemini', 'stub'"):
        llm.llm_from_env()


def test_llm_from_env_needs_the_prompt_file(env, tmp_path):
    env.setenv("NANOGPT_API_KEY", "key")
    env.setattr(llm, "DEFAULT_PROMPT_FILE", tmp_path / "missing.txt")
    with pytest.raises(ConfigError, match="missing.txt is missing"):
        llm.llm_from_env()
