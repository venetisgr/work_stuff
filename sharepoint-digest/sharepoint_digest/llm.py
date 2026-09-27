"""GPT models deployed in Azure AI Foundry, called through the Azure OpenAI v1 API."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse

import openai

from .config import ConfigError, FoundrySettings

if TYPE_CHECKING:
    import httpx2

log = logging.getLogger(__name__)

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_AZURE_HOST_SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
# Errors that repeat on every request, so there's no point carrying on with the other files.
_SETUP_ERROR_CODES = {"unsupported_parameter", "unsupported_value", "OperationNotSupported"}


class LLMError(Exception):
    """The model couldn't produce a reply for this input (content filter, empty or truncated reply...)."""


class LLMSetupError(Exception):
    """Every request will fail the same way (endpoint, deployment, credentials), so the run should stop."""


class ChatModel(Protocol):
    """Anything that turns a system prompt and a user prompt into text. Tests use a fake."""

    deployment: str

    def complete(self, system: str, prompt: str) -> str: ...


def foundry_base_url(endpoint: str) -> str:
    """Turn a resource name or endpoint URL from the Azure portal into the OpenAI v1 base URL."""
    endpoint = endpoint.strip()
    if "://" not in endpoint:  # just the resource name
        return f"https://{endpoint}.openai.azure.com/openai/v1/"
    parsed = urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/openai/v1"):
        return f"{parsed.scheme}://{parsed.netloc}{path}/"
    if (parsed.hostname or "").endswith(_AZURE_HOST_SUFFIXES):
        # Drops paths like /api/projects/<name> from a Foundry project endpoint.
        return f"{parsed.scheme}://{parsed.netloc}/openai/v1/"
    return f"{parsed.scheme}://{parsed.netloc}{path}/openai/v1/"  # e.g. an API Management gateway


def _entra_token_provider() -> Callable[[], str]:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_SCOPE)


class FoundryChatModel:
    """A chat model deployment (GPT-4o, GPT-4.1, GPT-5, o-series...) in an Azure AI Foundry resource."""

    def __init__(self, settings: FoundrySettings, *, http_client: httpx2.Client | None = None):
        if not settings.endpoint:
            raise ConfigError("Set FOUNDRY_ENDPOINT in .env to your Foundry resource's endpoint or name.")
        if not settings.deployment:
            raise ConfigError("Set FOUNDRY_DEPLOYMENT in .env to the name of your GPT model deployment.")
        self.deployment = settings.deployment
        self._reasoning_effort = settings.reasoning_effort
        self._max_output_tokens = settings.max_output_tokens
        self._client = openai.OpenAI(
            base_url=foundry_base_url(settings.endpoint),
            # A key if one is set, otherwise Entra ID (az login, managed identity, AZURE_* service principal).
            api_key=settings.api_key or _entra_token_provider(),
            max_retries=6,  # throttling (429) is common on shared deployments; the SDK honours retry-after
            http_client=http_client,
        )

    def complete(self, system: str, prompt: str) -> str:
        options: dict[str, object] = {}
        if self._reasoning_effort:
            options["reasoning_effort"] = self._reasoning_effort
        if self._max_output_tokens:
            options["max_completion_tokens"] = self._max_output_tokens
        try:
            response = self._client.chat.completions.create(
                model=self.deployment,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                **options,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMSetupError(
                f"Foundry rejected the credentials ({exc.status_code}). Check FOUNDRY_API_KEY, or for Entra ID "
                "that your identity has the Cognitive Services OpenAI User role on the resource."
            ) from exc
        except openai.NotFoundError as exc:
            raise LLMSetupError(
                f'No deployment named "{self.deployment}" at {self._client.base_url}. '
                "Check FOUNDRY_DEPLOYMENT (the deployment name, not the model name) and FOUNDRY_ENDPOINT."
            ) from exc
        except openai.APITimeoutError as exc:
            raise LLMError("The request to Foundry timed out.") from exc
        except openai.APIConnectionError as exc:
            raise LLMSetupError(f"Couldn't connect to {self._client.base_url}. Check FOUNDRY_ENDPOINT.") from exc
        except openai.RateLimitError as exc:
            raise LLMError(
                "Foundry kept throttling requests (429). Try fewer --workers or raise the deployment's quota."
            ) from exc
        except openai.BadRequestError as exc:
            if exc.code == "content_filter":
                raise LLMError("Azure's content filter blocked this text.") from exc
            if exc.code == "context_length_exceeded":
                raise LLMError("The text is too long for the model; lower LLM_MAX_INPUT_CHARS.") from exc
            if exc.code in _SETUP_ERROR_CODES:
                raise LLMSetupError(f"The deployment rejected the request settings: {exc.message}") from exc
            raise LLMError(f"Foundry rejected the request: {exc.message}") from exc

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if choice.finish_reason == "content_filter":
            raise LLMError("Azure's content filter blocked the model's reply.")
        if choice.finish_reason == "length":
            if not text:
                raise LLMError("The model ran out of output tokens before replying; raise LLM_MAX_OUTPUT_TOKENS.")
            log.warning("A reply hit the output token limit and may be cut short; raise LLM_MAX_OUTPUT_TOKENS.")
        if not text:
            raise LLMError("The model returned an empty reply.")
        return text
