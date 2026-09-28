"""Tests for the chat models and JSON helpers. The real SDKs run against an in-memory httpx2 transport: no network."""

from __future__ import annotations

import json
import logging
import sys

import httpx2
import pytest
from conftest import FakeChatModel

from dip_scanner import llm
from dip_scanner.config import ConfigError, LLMSettings
from dip_scanner.llm import (
    AnthropicChatModel,
    AzureFoundryChatModel,
    LLMError,
    LLMRequestError,
    LLMSetupError,
    LLMUnavailableError,
    OpenAIChatModel,
    Usage,
    build_models,
    complete_json,
    deployment_from_endpoint,
    extract_json,
    foundry_base_url,
)

_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
)


@pytest.fixture(autouse=True)
def clean_sdk_environment(monkeypatch):
    """The SDKs read these variables themselves; keep the machine's own settings out of the tests."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- extract_json --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("  [1, 2]  ", [1, 2]),
        ('```json\n{"a": {"b": [1, 2]}}\n```', {"a": {"b": [1, 2]}}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Here is the result:\n{"a": 1}\nHope this helps!', {"a": 1}),
        ('Sure. ```json\n{"ok": true}\n``` Let me know.', {"ok": True}),
        (
            '{"text": "braces } and { inside strings", "n": 2} trailing',
            {"text": "braces } and { inside strings", "n": 2},
        ),
        ('I found [2] items: {"articles": [{"id": "a1"}]}', {"articles": [{"id": "a1"}]}),
        ('Example {"x": 1} then the answer {"articles": [], "note": "longer"}', {"articles": [], "note": "longer"}),
        ('﻿{"a": "caf\\u00e9"}', {"a": "café"}),
    ],
)
def test_extract_json_finds_the_outermost_object_or_array(text, expected):
    assert extract_json(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "no json here", "42", '"just a string"', '{"a": [1, 2', "{'a': 1}"])
def test_extract_json_raises_value_error_without_a_json_object_or_array(text):
    with pytest.raises(ValueError):
        extract_json(text)


# --- complete_json -------------------------------------------------------------------------------------------------


def test_complete_json_parses_the_reply_in_json_mode():
    model = FakeChatModel(['Sure: {"a": 1}'])
    assert complete_json(model, "system", "prompt") == {"a": 1}
    assert model.calls == [("system", "prompt", True)]


def test_complete_json_retries_once_with_the_error_and_previous_reply():
    model = FakeChatModel(["not json at all", '{"a": 2}'])

    assert complete_json(model, "system", "prompt") == {"a": 2}

    assert len(model.calls) == 2
    system, retry_prompt, json_mode = model.calls[1]
    assert system == "system" and json_mode
    assert retry_prompt.startswith("prompt")
    assert "not json at all" in retry_prompt
    assert "No JSON object or array found" in retry_prompt
    assert retry_prompt.rstrip().endswith("Reply with only the corrected JSON.")


def test_complete_json_retries_when_validation_fails_and_returns_the_validated_value():
    def validate(data):
        if "answer" not in data:
            raise ValueError('missing "answer"')
        return data["answer"]

    model = FakeChatModel([{"wrong": 1}, {"answer": 42}])
    assert complete_json(model, "s", "p", validate=validate) == 42
    assert 'missing "answer"' in model.prompts[1]


def test_complete_json_gives_up_after_one_retry():
    model = FakeChatModel(["nope", "still nope", '{"never": "used"}'])
    with pytest.raises(LLMError, match="even after a corrective retry"):
        complete_json(model, "s", "p")
    assert len(model.calls) == 2


def test_complete_json_quotes_only_the_start_of_a_long_bad_reply():
    model = FakeChatModel(["x" * 20_000, "{}"])
    complete_json(model, "s", "p")
    assert "[... cut ...]" in model.prompts[1]
    assert len(model.prompts[1]) < 7_000


def test_complete_json_does_not_retry_model_errors():
    model = FakeChatModel([LLMError("content filter"), "{}"])
    with pytest.raises(LLMError, match="content filter"):
        complete_json(model, "s", "p")
    assert len(model.calls) == 1


# --- a mock HTTP transport shared by the OpenAI, Foundry and Anthropic tests ---------------------------------------


class Recorder:
    """A mock transport that records requests and answers from a list of responses (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return httpx2.Response(200, json=response)
        return response

    @property
    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


def completion(content="A reply.", finish_reason="stop", refusal=None) -> dict:
    message = {"role": "assistant", "content": content, "refusal": refusal}
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5-mini",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def openai_error(status: int, code: str | None, message: str = "Something went wrong", param=None) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"code": code, "message": message, "param": param}})


# --- OpenAI --------------------------------------------------------------------------------------------------------


def openai_settings(**overrides) -> LLMSettings:
    return LLMSettings(**{"provider": "openai", "openai_api_key": "sk-test", **overrides})


def openai_model(recorder: Recorder, model="gpt-5-mini", **overrides) -> OpenAIChatModel:
    return OpenAIChatModel(openai_settings(**overrides), model, http_client=recorder.client, max_retries=0)


def test_openai_sends_system_and_user_messages_to_chat_completions():
    recorder = Recorder(completion("  The answer.  "))

    model = openai_model(recorder)
    assert model.name == "gpt-5-mini"
    assert model.complete("Be brief.", "What now?") == "The answer."

    request = recorder.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert recorder.body == {
        "model": "gpt-5-mini",
        "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "What now?"}],
    }


def test_openai_json_mode_asks_for_a_json_object():
    recorder = Recorder(completion('{"a": 1}'))
    openai_model(recorder).complete("Reply in JSON.", "p", json_mode=True)
    assert recorder.body["response_format"] == {"type": "json_object"}
    assert recorder.body["messages"][0]["content"] == "Reply in JSON."


def test_openai_json_mode_adds_the_word_json_when_the_prompts_lack_it():
    recorder = Recorder(completion("{}"))
    openai_model(recorder).complete("Be brief.", "Answer.", json_mode=True)
    assert "JSON" in recorder.body["messages"][0]["content"]


def test_openai_falls_back_to_prompt_only_json_when_json_mode_is_unsupported(caplog):
    rejected = openai_error(400, "invalid_parameter", "response_format is not supported", param="response_format")
    recorder = Recorder(rejected, completion('{"a": 1}'))
    model = openai_model(recorder)

    assert model.complete("json please", "p", json_mode=True) == '{"a": 1}'
    assert "response_format" not in recorder.body
    assert "doesn't support JSON mode" in caplog.text

    model.complete("json please", "p", json_mode=True)  # remembered: no second rejected request
    assert len(recorder.requests) == 3
    assert "response_format" not in recorder.body


def test_openai_sends_reasoning_effort_and_output_cap_when_set():
    recorder = Recorder(completion())
    openai_model(recorder, reasoning_effort="low", max_output_tokens=900).complete("s", "p")
    assert recorder.body["reasoning_effort"] == "low"
    assert recorder.body["max_completion_tokens"] == 900


def test_openai_base_url_points_at_a_compatible_gateway_and_the_key_is_optional_there():
    recorder = Recorder(completion())
    openai_model(recorder, openai_api_key=None, openai_base_url="http://localhost:11434/v1").complete("s", "p")
    assert str(recorder.requests[0].url) == "http://localhost:11434/v1/chat/completions"


def test_openai_needs_an_api_key():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        OpenAIChatModel(openai_settings(openai_api_key=None), "gpt-5-mini")


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (openai_error(401, "invalid_api_key"), LLMSetupError, "OPENAI_API_KEY"),
        (openai_error(403, None), LLMSetupError, "rejected the credentials"),
        (openai_error(404, "model_not_found"), LLMSetupError, 'no model named "gpt-5-mini"'),
        (openai_error(429, "insufficient_quota"), LLMSetupError, "no quota"),
        (openai_error(429, "rate_limit_exceeded"), LLMUnavailableError, "throttling"),
        (openai_error(500, None), LLMUnavailableError, "server error"),
        (openai_error(400, "content_filter"), LLMError, "content filter blocked this text"),
        (openai_error(400, "context_length_exceeded"), LLMError, "too long"),
        (openai_error(400, "unsupported_value", "reasoning_effort 'max' is not supported"), LLMSetupError, "max"),
        (openai_error(400, "invalid_request_error", "Bad messages"), LLMError, "Bad messages"),
        (openai_error(409, None, "Conflict here"), LLMError, "Conflict here"),
        (completion(None, "content_filter"), LLMError, "content filter blocked the model's reply"),
        (completion("", "length"), LLMError, "LLM_MAX_OUTPUT_TOKENS"),
        (completion(""), LLMError, "empty reply"),
        (completion(None, refusal="I can't help with that."), LLMError, "declined to answer: I can't help"),
        (httpx2.ConnectError("refused"), LLMUnavailableError, "Couldn't connect to https://api.openai.com/v1/"),
        (httpx2.ReadTimeout("slow"), LLMUnavailableError, "timed out"),
    ],
)
def test_openai_failures_map_to_input_service_or_setup_errors(response, error, message):
    with pytest.raises(error, match=message):
        openai_model(Recorder(response)).complete("s", "p")


def test_throttling_and_outages_are_llm_errors_too():
    assert issubclass(LLMUnavailableError, LLMError)
    assert issubclass(LLMRequestError, LLMError)
    assert not issubclass(LLMSetupError, LLMError)


@pytest.mark.parametrize(
    "response",
    [openai_error(400, "invalid_request_error", "Bad messages"), openai_error(409, None, "Conflict here")],
)
def test_unexplained_refusals_are_request_errors(response):
    """So triage can tell "every request is refused" from "this article is bad" (see triage())."""
    with pytest.raises(LLMRequestError):
        openai_model(Recorder(response)).complete("s", "p")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": []}, r"no reply \(no choices"),
        ({"error": {"message": "upstream failed", "code": 502}}, "no reply: .*upstream failed"),
        ({"choices": None}, "no reply"),
        ({"choices": [{"index": 0, "finish_reason": "stop"}]}, "no reply"),
    ],
)
def test_a_gateway_reply_without_choices_is_an_llm_error(body, message):
    """Regression: an OpenAI-compatible gateway answering 200 with no choices raised IndexError/TypeError, which
    escaped triage, so the same batch was retried every cycle and the run never got further."""
    with pytest.raises(LLMError, match=message):
        openai_model(Recorder(body)).complete("s", "p")


def test_a_blank_base_url_in_the_environment_means_the_default(monkeypatch):
    """Regression: `OPENAI_BASE_URL=` in .env made every request go to "" ("Couldn't connect to .")."""
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    recorder = Recorder(completion())
    openai_model(recorder).complete("s", "p")
    assert str(recorder.requests[0].url) == "https://api.openai.com/v1/chat/completions"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", " ")
    recorder = Recorder(message())
    anthropic_model(recorder).complete("s", "p")
    assert str(recorder.requests[0].url) == "https://api.anthropic.com/v1/messages"


def test_openai_keeps_a_truncated_reply_with_a_warning(caplog):
    assert openai_model(Recorder(completion("Partial", "length"))).complete("s", "p") == "Partial"
    assert "output token limit" in caplog.text


def test_openai_reports_the_tokens_of_every_answered_call():
    model = openai_model(Recorder(completion()))
    assert model.last_usage is None
    model.complete("s", "p")
    assert model.last_usage == Usage(input_tokens=10, output_tokens=5)

    # A reply that is unusable was still answered (and billed): its tokens are kept.
    filtered = openai_model(Recorder(completion("", "content_filter")))
    with pytest.raises(LLMError):
        filtered.complete("s", "p")
    assert filtered.last_usage == Usage(10, 5)

    # A gateway that reports no usage: answered, tokens unknown.
    body = completion()
    del body["usage"]
    model = openai_model(Recorder(body))
    model.complete("s", "p")
    assert model.last_usage == Usage(None, None)

    # No reply at all: nothing was used, and the previous call's figures don't linger.
    model = openai_model(Recorder(completion(), openai_error(500, None)))
    model.complete("s", "p")
    with pytest.raises(LLMUnavailableError):
        model.complete("s", "p")
    assert model.last_usage is None


# --- Azure AI Foundry ----------------------------------------------------------------------------------------------


def foundry_settings(**overrides) -> LLMSettings:
    values = {"provider": "azure", "foundry_endpoint": "my-resource", "foundry_api_key": "test-key"}
    return LLMSettings(**{**values, **overrides})


def foundry_model(recorder: Recorder, deployment="gpt-5-mini", **overrides) -> AzureFoundryChatModel:
    return AzureFoundryChatModel(foundry_settings(**overrides), deployment, http_client=recorder.client, max_retries=0)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("my-resource", "https://my-resource.openai.azure.com/openai/v1/"),
        ("https://my-resource.openai.azure.com/", "https://my-resource.openai.azure.com/openai/v1/"),
        (
            "https://my-resource.services.ai.azure.com/api/projects/my-project",
            "https://my-resource.services.ai.azure.com/openai/v1/",
        ),
        ("https://my-resource.openai.azure.com/openai/v1", "https://my-resource.openai.azure.com/openai/v1/"),
        ("https://gateway.contoso.com/aoai", "https://gateway.contoso.com/aoai/openai/v1/"),
        (
            "https://my-resource.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2025-01-01",
            "https://my-resource.openai.azure.com/openai/v1/",
        ),
    ],
)
def test_foundry_base_url_accepts_names_and_portal_endpoints(endpoint, expected):
    assert foundry_base_url(endpoint) == expected


def test_deployment_from_endpoint_reads_a_pasted_target_uri():
    uri = "https://r.openai.azure.com/openai/deployments/my%20gpt/chat/completions?api-version=2025-01-01"
    assert deployment_from_endpoint(uri) == "my gpt"
    assert deployment_from_endpoint("https://r.openai.azure.com/") is None


def test_foundry_calls_the_v1_api_with_the_deployment_name():
    recorder = Recorder(completion("ok"))

    model = foundry_model(recorder, deployment="scanner-gpt5")
    assert model.name == "scanner-gpt5"
    assert model.complete("s", "p", json_mode=True) == "ok"

    request = recorder.requests[0]
    assert str(request.url) == "https://my-resource.openai.azure.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert recorder.body["model"] == "scanner-gpt5"
    assert recorder.body["response_format"] == {"type": "json_object"}


def test_foundry_falls_back_to_foundry_deployment_and_the_target_uri():
    recorder = Recorder(completion())
    assert foundry_model(recorder, deployment="", foundry_deployment="fallback").name == "fallback"
    uri = "https://my-resource.openai.azure.com/openai/deployments/from-uri/chat/completions"
    assert foundry_model(recorder, deployment="", foundry_endpoint=uri).name == "from-uri"


def test_foundry_without_an_api_key_signs_in_with_entra_id(monkeypatch):
    monkeypatch.setattr(llm, "_entra_token_provider", lambda: lambda: "entra-token")
    recorder = Recorder(completion())
    foundry_model(recorder, foundry_api_key=None).complete("s", "p")
    assert recorder.requests[0].headers["authorization"] == "Bearer entra-token"


def test_a_failed_entra_id_sign_in_is_a_setup_error(monkeypatch):
    """Regression: with no key and no az login, azure's ClientAuthenticationError escaped as a raw traceback."""
    import azure.identity
    from azure.core.exceptions import ClientAuthenticationError

    def provider(credential, scope):
        def token():
            raise ClientAuthenticationError("DefaultAzureCredential failed to retrieve a token.\nDetails...")

        return token

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: object())
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", provider)
    model = foundry_model(Recorder(completion()), foundry_api_key=None)
    with pytest.raises(LLMSetupError, match="az login.*DefaultAzureCredential failed to retrieve a token"):
        model.complete("s", "p")


def test_entra_id_without_azure_identity_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "azure.identity", None)  # makes the import fail
    with pytest.raises(ConfigError, match=r"news-dip-scanner\[azure\]"):
        AzureFoundryChatModel(foundry_settings(foundry_api_key=None), "gpt-5-mini")


@pytest.mark.parametrize(
    ("overrides", "deployment", "message"),
    [({"foundry_endpoint": None}, "gpt", "FOUNDRY_ENDPOINT"), ({}, "", "FOUNDRY_DEPLOYMENT")],
)
def test_foundry_needs_an_endpoint_and_a_deployment(overrides, deployment, message):
    with pytest.raises(ConfigError, match=message):
        AzureFoundryChatModel(foundry_settings(**overrides), deployment)


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (openai_error(401, "401"), LLMSetupError, "Cognitive Services OpenAI User"),
        (openai_error(404, "DeploymentNotFound"), LLMSetupError, 'No deployment named "gpt-5-mini"'),
        (openai_error(429, "429"), LLMUnavailableError, "Foundry kept throttling"),
        (openai_error(400, "content_filter"), LLMError, "Foundry's content filter"),
        (httpx2.ConnectError("dns"), LLMUnavailableError, "FOUNDRY_ENDPOINT"),
    ],
)
def test_foundry_errors_name_foundry_settings(response, error, message):
    with pytest.raises(error, match=message):
        foundry_model(Recorder(response)).complete("s", "p")


# --- Anthropic -----------------------------------------------------------------------------------------------------


def message(*blocks, stop_reason="end_turn", stop_details=None) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": list(blocks) or [{"type": "text", "text": "A reply."}],
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def anthropic_error(status: int, kind: str, text_: str = "Something went wrong") -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": text_}})


def anthropic_settings(**overrides) -> LLMSettings:
    return LLMSettings(**{"provider": "anthropic", "anthropic_api_key": "sk-ant-test", **overrides})


def anthropic_model(recorder: Recorder, model="claude-sonnet-5", **overrides) -> AnthropicChatModel:
    return AnthropicChatModel(anthropic_settings(**overrides), model, http_client=recorder.client, max_retries=0)


def test_anthropic_sends_system_and_one_user_message():
    recorder = Recorder(message(text("  Hello.  ")))

    model = anthropic_model(recorder)
    assert model.name == "claude-sonnet-5"
    assert model.complete("Be brief.", "Hi") == "Hello."

    request = recorder.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert recorder.body == {
        "model": "claude-sonnet-5",
        "max_tokens": llm.ANTHROPIC_MAX_TOKENS,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "Hi"}],
    }


def test_anthropic_json_mode_asks_in_the_system_prompt_and_never_prefills():
    recorder = Recorder(message(text('{"a": 1}')))
    anthropic_model(recorder).complete("Be brief.", "Hi", json_mode=True)
    assert recorder.body["system"].startswith("Be brief.")
    assert "single JSON object" in recorder.body["system"]
    assert [m["role"] for m in recorder.body["messages"]] == ["user"]


def test_anthropic_joins_text_blocks_and_skips_thinking():
    recorder = Recorder(message({"type": "thinking", "thinking": "", "signature": "sig"}, text('{"a": '), text("1}")))
    assert anthropic_model(recorder).complete("s", "p") == '{"a": 1}'


def test_anthropic_output_cap_and_effort_come_from_the_settings():
    recorder = Recorder(message())
    anthropic_model(recorder, max_output_tokens=2000, reasoning_effort="low").complete("s", "p")
    assert recorder.body["max_tokens"] == 2000
    assert recorder.body["output_config"] == {"effort": "low"}


def test_anthropic_does_not_send_effort_to_haiku_or_unknown_effort_levels(caplog):
    recorder = Recorder(message())
    anthropic_model(recorder, model="claude-haiku-4-5", reasoning_effort="low").complete("s", "p")
    assert "output_config" not in recorder.body

    anthropic_model(recorder, reasoning_effort="minimal").complete("s", "p")
    assert "output_config" not in recorder.body
    assert "isn't a Claude effort level" in caplog.text


def test_anthropic_caps_max_tokens_at_the_non_streaming_limit(caplog):
    recorder = Recorder(message())
    anthropic_model(recorder, max_output_tokens=64_000).complete("s", "p")
    assert recorder.body["max_tokens"] == 21_000
    assert "without streaming" in caplog.text


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (
            message(stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}),
            LLMError,
            r"declined to answer \(cyber\)",
        ),
        (message(text(""), stop_reason="max_tokens"), LLMError, "LLM_MAX_OUTPUT_TOKENS"),
        (message(text("  ")), LLMError, "empty reply"),
        (anthropic_error(401, "authentication_error", "invalid x-api-key"), LLMSetupError, "ANTHROPIC_API_KEY"),
        (anthropic_error(403, "permission_error"), LLMSetupError, "rejected the credentials"),
        (anthropic_error(404, "not_found_error", "model: claude-nope"), LLMSetupError, 'no model named "claude-sonnet'),
        (anthropic_error(402, "billing_error", "credit balance too low"), LLMSetupError, "credit balance"),
        (anthropic_error(429, "rate_limit_error"), LLMUnavailableError, "throttling"),
        (anthropic_error(529, "overloaded_error"), LLMUnavailableError, "overloaded"),
        (anthropic_error(500, "api_error"), LLMUnavailableError, "server error"),
        (anthropic_error(400, "invalid_request_error", "prompt is too long: 300000 tokens"), LLMError, "too long"),
        (anthropic_error(400, "invalid_request_error", "effort: not supported"), LLMSetupError, "LLM_REASONING"),
        (anthropic_error(400, "invalid_request_error", "messages: bad"), LLMRequestError, "messages: bad"),
        # Regression: account-level 400s were per-input errors, so every pending article failed for good.
        (
            anthropic_error(400, "invalid_request_error", "You have reached your specified API usage limits."),
            LLMSetupError,
            "usage-limit",
        ),
        (
            anthropic_error(400, "invalid_request_error", "Your credit balance is too low to access the API."),
            LLMSetupError,
            "billing",
        ),
        # Regression: a per-input context overflow mentions max_tokens but isn't a settings problem.
        (
            anthropic_error(
                400, "invalid_request_error", "input length and `max_tokens` exceed context limit: 190000 + 16000"
            ),
            LLMError,
            "too long for the model's context window",
        ),
        (message(text(""), stop_reason="model_context_window_exceeded"), LLMError, "context window"),
        (anthropic_error(413, "request_too_large"), LLMError, "too large"),
        (httpx2.ConnectError("refused"), LLMUnavailableError, "Couldn't connect"),
        (httpx2.ReadTimeout("slow"), LLMUnavailableError, "timed out"),
    ],
)
def test_anthropic_failures_map_to_input_service_or_setup_errors(response, error, match):
    with pytest.raises(error, match=match):
        anthropic_model(Recorder(response)).complete("s", "p")


def test_anthropic_context_overflow_is_not_a_setup_error():
    response = anthropic_error(400, "invalid_request_error", "input length and `max_tokens` exceed context limit")
    with pytest.raises(LLMError) as caught:
        anthropic_model(Recorder(response)).complete("s", "p")
    assert not isinstance(caught.value, LLMSetupError)


@pytest.mark.filterwarnings("ignore:The model 'claude-opus-4-0' is deprecated:DeprecationWarning")
def test_anthropic_respects_the_sdks_non_streaming_limit_of_older_models(caplog):
    """Regression: claude-opus-4-0 with the default 16,000 tokens raised the SDK's plain ValueError."""
    recorder = Recorder(message())
    anthropic_model(recorder, model="claude-opus-4-0").complete("s", "p")
    assert recorder.body["max_tokens"] == 8192
    assert "without streaming" in caplog.text


def test_anthropic_streaming_refusals_are_setup_errors():
    class Messages:
        def create(self, **kwargs):
            raise ValueError("Streaming is required for operations that may take longer than 10 minutes.")

    client = type("Client", (), {"messages": Messages()})()
    model = AnthropicChatModel(anthropic_settings(), "claude-future", client=client)
    with pytest.raises(LLMSetupError, match="needs streaming"):
        model.complete("s", "p")


def test_anthropic_keeps_a_truncated_reply_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        reply = anthropic_model(Recorder(message(text("Partial"), stop_reason="max_tokens"))).complete("s", "p")
    assert reply == "Partial"
    assert "output token limit" in caplog.text


def test_anthropic_reports_the_tokens_with_cached_input_included():
    body = message()
    body["usage"] = {
        "input_tokens": 40,
        "output_tokens": 700,
        "cache_creation_input_tokens": 1_000,
        "cache_read_input_tokens": None,
    }
    model = anthropic_model(Recorder(body))
    model.complete("s", "p")
    assert model.last_usage == Usage(input_tokens=1_040, output_tokens=700)

    refused = anthropic_model(Recorder(message(stop_reason="refusal")))
    with pytest.raises(LLMError):
        refused.complete("s", "p")
    assert refused.last_usage == Usage(10, 5)  # declined, but answered

    failing = anthropic_model(Recorder(anthropic_error(529, "overloaded_error")))
    with pytest.raises(LLMUnavailableError):
        failing.complete("s", "p")
    assert failing.last_usage is None


def test_anthropic_accepts_an_injected_client():
    class Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            block = type("Block", (), {"type": "text", "text": "hi"})()
            return type("Response", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    client = type("Client", (), {"messages": Messages()})()
    model = AnthropicChatModel(anthropic_settings(anthropic_api_key=None), "claude-haiku-4-5", client=client)
    assert model.complete("s", "p") == "hi"
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"


def test_anthropic_without_credentials_is_a_config_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.config/anthropic profile either
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicChatModel(anthropic_settings(anthropic_api_key=None), "claude-haiku-4-5")

    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicChatModel(anthropic_settings(anthropic_api_key=None), "claude-haiku-4-5")


def test_anthropic_without_the_package_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes the import fail
    with pytest.raises(ConfigError, match=r"news-dip-scanner\[anthropic\]"):
        AnthropicChatModel(anthropic_settings(), "claude-haiku-4-5")


# --- build_models --------------------------------------------------------------------------------------------------


def test_build_models_uses_the_provider_defaults():
    triage, analysis = build_models(openai_settings())
    assert isinstance(triage, OpenAIChatModel) and isinstance(analysis, OpenAIChatModel)
    assert (triage.name, analysis.name) == ("gpt-5-mini", "gpt-5")

    triage, analysis = build_models(anthropic_settings())
    assert isinstance(triage, AnthropicChatModel)
    assert (triage.name, analysis.name) == ("claude-haiku-4-5", "claude-sonnet-5")


def test_build_models_honours_the_model_settings_and_shares_one_model_when_they_match():
    triage, analysis = build_models(openai_settings(triage_model="gpt-5-nano", analysis_model="gpt-5.1"))
    assert (triage.name, analysis.name) == ("gpt-5-nano", "gpt-5.1")

    triage, analysis = build_models(openai_settings(triage_model="gpt-5", analysis_model="gpt-5"))
    assert triage is analysis


def test_each_step_can_have_its_own_reasoning_effort():
    recorder = Recorder(completion())
    settings = openai_settings(
        reasoning_effort="medium", triage_reasoning_effort="low", triage_model="gpt-5", analysis_model="gpt-5"
    )
    triage, analysis = build_models(settings)
    assert triage is not analysis  # the same model name, but two efforts
    for model, effort in ((triage, "low"), (analysis, "medium")):  # the analysis falls back to the shared one
        model._client = model._client.with_options(http_client=recorder.client, max_retries=0)
        model.complete("s", "p")
        assert recorder.body["reasoning_effort"] == effort

    triage, analysis = build_models(openai_settings(analysis_reasoning_effort="high"))
    assert (triage._reasoning_effort, analysis._reasoning_effort) == (None, "high")

    triage, analysis = build_models(anthropic_settings(reasoning_effort="low", analysis_reasoning_effort="max"))
    assert (triage._effort, analysis._effort) == (None, "max")  # Haiku gets no effort at all


def test_build_models_for_azure_uses_deployment_names():
    triage, analysis = build_models(foundry_settings(triage_model="cheap", analysis_model="smart"))
    assert isinstance(triage, AzureFoundryChatModel)
    assert (triage.name, analysis.name) == ("cheap", "smart")

    triage, analysis = build_models(foundry_settings(foundry_deployment="both"))
    assert triage is analysis and triage.name == "both"

    triage, analysis = build_models(foundry_settings(foundry_deployment="both", analysis_model="smart"))
    assert (triage.name, analysis.name) == ("both", "smart")

    uri = "https://my-resource.openai.azure.com/openai/deployments/pasted/chat/completions"
    assert build_models(foundry_settings(foundry_endpoint=uri))[0].name == "pasted"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (foundry_settings(), "FOUNDRY_DEPLOYMENT"),
        (foundry_settings(foundry_endpoint=None, foundry_deployment="x"), "FOUNDRY_ENDPOINT"),
        (openai_settings(openai_api_key=None), "OPENAI_API_KEY"),
        (LLMSettings(provider="mystery"), "Unknown LLM_PROVIDER"),
    ],
)
def test_build_models_says_what_is_missing(settings, message):
    with pytest.raises(ConfigError, match=message):
        build_models(settings)


# --- the debate's models (LLM_ANALYSIS_MODE=debate) ------------------------------------------------------------------


def debate_settings(**overrides) -> LLMSettings:
    values = {
        "provider": "openai",
        "openai_api_key": "sk-test",
        "anthropic_api_key": "sk-ant-test",
        "analysis_mode": "debate",
    }
    return LLMSettings(**{**values, **overrides})


def test_build_models_in_debate_mode_returns_the_panel_and_keeps_triage():
    triage, panel = build_models(debate_settings())
    assert isinstance(triage, OpenAIChatModel) and triage.name == "gpt-5-mini"
    assert isinstance(panel, llm.DebatePanel)
    first, second = panel.debaters
    assert (first.label, first.provider, first.model_name) == ("openai:gpt-5", "openai", "gpt-5")
    assert isinstance(first.model, OpenAIChatModel) and first.model.name == "gpt-5"
    assert (second.label, second.provider) == ("anthropic:claude-sonnet-5", "anthropic")
    assert isinstance(second.model, AnthropicChatModel) and second.model.name == "claude-sonnet-5"
    assert panel.judge is None  # alternate: the debaters take turns
    assert panel.name == "debate: gpt-5 vs claude-sonnet-5"


def test_the_debaters_and_the_judge_use_the_analysis_effort():
    triage, panel = build_models(debate_settings(triage_reasoning_effort="low", analysis_reasoning_effort="high"))
    assert triage._reasoning_effort == "low"
    first, second = panel.debaters
    assert (first.model._reasoning_effort, second.model._effort) == ("high", "high")

    _, panel = build_models(debate_settings(debate_judge="anthropic:claude-opus-5", reasoning_effort="medium"))
    assert panel.judge.label == "anthropic:claude-opus-5" and panel.judge.model._effort == "medium"
    assert panel.name == "debate: gpt-5 vs claude-sonnet-5, judged by claude-opus-5"


def test_a_fixed_judge_that_is_a_debater_shares_its_model():
    _, panel = build_models(debate_settings(debate_judge="openai:gpt-5"))
    assert panel.judge is panel.debaters[0]


def test_debaters_of_any_provider_and_order(monkeypatch):
    settings = debate_settings(
        debaters=("anthropic:claude-sonnet-5", "azure:my-gpt"), foundry_endpoint="my-resource", foundry_api_key="k"
    )
    _, panel = build_models(settings)
    assert [debater.label for debater in panel.debaters] == ["anthropic:claude-sonnet-5", "azure:my-gpt"]
    assert isinstance(panel.debaters[1].model, AzureFoundryChatModel) and panel.debaters[1].model.name == "my-gpt"


def test_a_debater_without_its_key_is_a_config_error_that_names_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.config/anthropic profile either
    with pytest.raises(ConfigError) as error:
        build_models(debate_settings(anthropic_api_key=None))
    message = str(error.value)
    expected = "LLM_ANALYSIS_MODE=debate uses anthropic:claude-sonnet-5 (LLM_DEBATERS), but ANTHROPIC_API_KEY isn't set"
    assert expected in message
    assert "fly secrets set ANTHROPIC_API_KEY=..." in message and "LLM_ANALYSIS_MODE=single" in message

    # Triage is built first, so without the OpenAI key its own message comes first; a debater's names the debate.
    with pytest.raises(ConfigError, match="Set OPENAI_API_KEY"):
        build_models(debate_settings(openai_api_key=None))
    with pytest.raises(ConfigError, match=r"uses openai:gpt-5 \(LLM_DEBATERS\), but OPENAI_API_KEY isn't set"):
        build_models(debate_settings(provider="anthropic", openai_api_key=None))
    with pytest.raises(ConfigError, match=r"uses azure:x \(LLM_DEBATE_JUDGE\), but FOUNDRY_ENDPOINT isn't set"):
        build_models(debate_settings(debate_judge="azure:x"))


def test_a_debater_without_its_package_says_which(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes the import fail
    with pytest.raises(ConfigError, match=r"uses anthropic:claude-sonnet-5 .* can't be set up: .*\[anthropic\]"):
        build_models(debate_settings())
