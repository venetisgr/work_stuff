import json

import httpx2
import pytest

from sharepoint_digest import llm
from sharepoint_digest.config import ConfigError, FoundrySettings
from sharepoint_digest.llm import FoundryChatModel, LLMError, LLMSetupError, foundry_base_url


def settings(**overrides) -> FoundrySettings:
    values = {
        "endpoint": "my-resource",
        "deployment": "gpt-4.1",
        "api_key": "test-key",
        "reasoning_effort": None,
        "max_output_tokens": None,
    }
    return FoundrySettings(**{**values, **overrides})


def completion(content="A summary.", finish_reason="stop") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4.1",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}],
    }


def api_error(status: int, code: str, message: str = "Something went wrong") -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"code": code, "message": message}})


class Recorder:
    """A mock transport that records requests and replies with a fixed response."""

    def __init__(self, response: httpx2.Response | dict):
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.response, dict):
            return httpx2.Response(200, json=self.response)
        return self.response

    def model(self, **overrides) -> FoundryChatModel:
        return FoundryChatModel(settings(**overrides), http_client=httpx2.Client(transport=httpx2.MockTransport(self)))

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("my-resource", "https://my-resource.openai.azure.com/openai/v1/"),
        ("https://my-resource.openai.azure.com/", "https://my-resource.openai.azure.com/openai/v1/"),
        ("https://my-resource.services.ai.azure.com", "https://my-resource.services.ai.azure.com/openai/v1/"),
        (
            "https://my-resource.services.ai.azure.com/api/projects/my-project",
            "https://my-resource.services.ai.azure.com/openai/v1/",
        ),
        (
            "https://my-resource.cognitiveservices.azure.com/",
            "https://my-resource.cognitiveservices.azure.com/openai/v1/",
        ),
        ("https://my-resource.openai.azure.com/openai/v1", "https://my-resource.openai.azure.com/openai/v1/"),
        ("https://gateway.contoso.com/aoai", "https://gateway.contoso.com/aoai/openai/v1/"),
    ],
)
def test_foundry_base_url_accepts_names_and_portal_endpoints(endpoint, expected):
    assert foundry_base_url(endpoint) == expected


def test_complete_calls_the_v1_chat_completions_api_with_the_deployment():
    recorder = Recorder(completion("  **Overview:** Fine.  "))

    assert recorder.model().complete("Be brief.", "Summarize this.") == "**Overview:** Fine."

    request = recorder.requests[0]
    assert str(request.url) == "https://my-resource.openai.azure.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert recorder.body == {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Summarize this."},
        ],
    }


def test_optional_reasoning_effort_and_output_cap_are_sent_when_set():
    recorder = Recorder(completion())
    recorder.model(reasoning_effort="low", max_output_tokens=900).complete("s", "p")
    assert recorder.body["reasoning_effort"] == "low"
    assert recorder.body["max_completion_tokens"] == 900


def test_without_an_api_key_it_signs_in_with_entra_id(monkeypatch):
    monkeypatch.setattr(llm, "_entra_token_provider", lambda: lambda: "entra-token")
    recorder = Recorder(completion())
    recorder.model(api_key=None).complete("s", "p")
    assert recorder.requests[0].headers["authorization"] == "Bearer entra-token"


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (api_error(401, "401", "Access denied"), LLMSetupError, "rejected the credentials"),
        (api_error(404, "DeploymentNotFound"), LLMSetupError, 'No deployment named "gpt-4.1"'),
        (
            api_error(400, "unsupported_parameter", "reasoning_effort is not supported"),
            LLMSetupError,
            "reasoning_effort",
        ),
        (api_error(400, "content_filter"), LLMError, "content filter blocked this text"),
        (api_error(400, "context_length_exceeded"), LLMError, "LLM_MAX_INPUT_CHARS"),
        (completion(None, "content_filter"), LLMError, "content filter blocked the model's reply"),
        (completion("", "length"), LLMError, "LLM_MAX_OUTPUT_TOKENS"),
        (completion(""), LLMError, "empty reply"),
    ],
)
def test_failures_are_reported_as_per_document_or_setup_errors(response, error, message):
    with pytest.raises(error, match=message):
        Recorder(response).model().complete("s", "p")


def test_a_truncated_reply_is_kept_with_a_warning(caplog):
    assert Recorder(completion("Partial summary", "length")).model().complete("s", "p") == "Partial summary"
    assert "output token limit" in caplog.text


@pytest.mark.parametrize("missing", ["endpoint", "deployment"])
def test_endpoint_and_deployment_are_required(missing):
    with pytest.raises(ConfigError, match=f"FOUNDRY_{missing.upper()}"):
        FoundryChatModel(settings(**{missing: None}))
