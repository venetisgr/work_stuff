"""Chat models (OpenAI, Azure AI Foundry, Anthropic) behind one small interface, plus JSON reply helpers.

Every model turns a system prompt and a user prompt into text. Failures come out as one of three errors:
- LLMSetupError: every request will fail the same way (credentials, unknown model, rejected settings): stop the run.
- LLMUnavailableError: the service can't be reached or keeps throttling; nothing is wrong with this input, so the
  caller should try again later rather than count it as a failed attempt. It is an LLMError, so code that only
  cares about "this call failed" can catch LLMError.
- LLMError: this input failed (unusable JSON after a retry, content filter, refusal, empty or truncated reply).
  Its subclass LLMRequestError is a request the service refused for a reason it didn't name more precisely (a 400
  or other 4xx): that can be this input or a setting every request shares, so callers shouldn't use up an
  article's attempts when every request is refused the same way.
Anything else the SDKs raise (an Entra ID sign-in that fails, a gateway reply without choices) is mapped to one of
these too.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import unquote, urlparse

import openai

from .config import DEFAULT_MODELS, ConfigError, LLMSettings

if TYPE_CHECKING:
    import httpx2

log = logging.getLogger(__name__)

T = TypeVar("T")

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
# Anthropic 400s that are about the account, not the request: every later request fails the same way.
_ANTHROPIC_ACCOUNT_REFUSAL = re.compile(r"credit balance|usage limit|spend limit|billing", re.IGNORECASE)
_ANTHROPIC_CONTEXT_OVERFLOW = re.compile(r"too long|context limit|context window|exceed context", re.IGNORECASE)
_AZURE_HOST_SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
# OpenAI error codes that repeat on every request, so there's no point carrying on.
_SETUP_ERROR_CODES = {"unsupported_parameter", "unsupported_value", "OperationNotSupported"}
_DEPLOYMENT_IN_URL = re.compile(r"/openai/deployments/([^/?#]+)")

# The SDKs retry throttling (429), 5xx and connection errors with backoff and honour retry-after.
_MAX_RETRIES = 5

# Anthropic needs max_tokens on every request. Thinking tokens count towards it (Sonnet 5 thinks by default), so
# leave room. Above ~21k tokens the SDK refuses non-streaming requests, so a larger LLM_MAX_OUTPUT_TOKENS is capped.
ANTHROPIC_MAX_TOKENS = 16_000
_ANTHROPIC_NONSTREAMING_MAX_TOKENS = 21_000
_ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

JSON_INSTRUCTION = "Reply with a single JSON object only: no code fences, no text before or after it."
_RETRY_INSTRUCTION = "Reply with only the corrected JSON."
_MAX_ECHOED_REPLY = 6000  # characters of a bad reply quoted back in the corrective retry


class LLMError(Exception):
    """This input failed (bad JSON after a retry, content filter, refusal, empty reply, timeout, throttling)."""


class LLMUnavailableError(LLMError):
    """The service couldn't be reached or kept throttling. Not this input's fault: try again later."""


class LLMRequestError(LLMError):
    """The service refused the request without saying why more precisely (a 400 or other 4xx): this input, or a
    setting every request shares."""


class LLMSetupError(Exception):
    """Every request will fail the same way (credentials, unknown model or deployment, endpoint): stop the run."""


class ChatModel(Protocol):
    """Anything that turns a system prompt and a user prompt into text. Tests use a fake."""

    name: str

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str: ...


# --- JSON replies --------------------------------------------------------------------------------------------------


def extract_json(text: str) -> Any:
    """Parse the outermost JSON object or array in a reply (code fences and leading prose are ignored).

    Raises ValueError when the reply holds no parsable JSON object or array.
    """
    text = (text or "").strip().lstrip("﻿")
    if not text:
        raise ValueError("The reply was empty.")
    try:
        data = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(data, dict | list):
            return data
    # Try every "{" or "[" as a start and keep the longest value that parses: that is the outermost one, and it
    # skips prose like "I found [2] items" before the real answer. raw_decode handles braces inside strings.
    decoder = json.JSONDecoder()
    best: Any = None
    best_length = 0
    index = 0
    while (match := _JSON_START.search(text, index)) is not None:
        start = match.start()
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if end - start > best_length:
            best, best_length = value, end - start
        index = end  # anything inside this value is shorter
    if best_length:
        return best
    snippet = " ".join(text.split())[:200]
    raise ValueError(f"No JSON object or array found in the reply (it starts: {snippet!r}).")


_JSON_START = re.compile(r"[{\[]")


def complete_json(model: ChatModel, system: str, prompt: str, *, validate: Callable[[Any], T] | None = None) -> T | Any:
    """Ask for JSON; on unparsable or invalid JSON retry once with the error, then raise LLMError.

    validate gets the parsed JSON and returns the value to hand back; it raises ValueError to reject the reply.
    """
    reply = model.complete(system, prompt, json_mode=True)
    try:
        return _parse(reply, validate)
    except ValueError as exc:
        error = exc
    log.info("%s returned unusable JSON (%s); asking it to correct the reply.", model.name, error)
    echoed = reply.strip()
    if len(echoed) > _MAX_ECHOED_REPLY:
        echoed = echoed[:_MAX_ECHOED_REPLY] + "\n[... cut ...]"
    retry_prompt = (
        f"{prompt}\n\n---\nYour previous reply was:\n{echoed}\n\n"
        f"That reply could not be used: {error}\n{_RETRY_INSTRUCTION}"
    )
    reply = model.complete(system, retry_prompt, json_mode=True)
    try:
        return _parse(reply, validate)
    except ValueError as exc:
        raise LLMError(f"{model.name} did not return usable JSON, even after a corrective retry: {exc}") from exc


def _parse(reply: str, validate: Callable[[Any], T] | None) -> T | Any:
    data = extract_json(reply)
    return validate(data) if validate is not None else data


# --- OpenAI and Azure AI Foundry -----------------------------------------------------------------------------------


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


def deployment_from_endpoint(endpoint: str) -> str | None:
    """The deployment name in a pasted Target URI, e.g. .../openai/deployments/<name>/chat/completions?..."""
    match = _DEPLOYMENT_IN_URL.search(endpoint)
    return unquote(match.group(1)) if match else None


def _entra_token_provider() -> Callable[[], str]:
    try:
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise ConfigError(
            "FOUNDRY_API_KEY is empty, so the scanner signs in to Foundry with Entra ID, which needs azure-identity: "
            'pip install "news-dip-scanner[azure]". Or set FOUNDRY_API_KEY in .env.'
        ) from exc
    provider = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_SCOPE)

    def token() -> str:
        # The SDK calls this for every request and lets its errors through unchanged.
        try:
            return provider()
        except ClientAuthenticationError as exc:  # also CredentialUnavailableError: no az login, no identity
            raise LLMSetupError(
                "FOUNDRY_API_KEY is empty and signing in to Foundry with Entra ID failed (run `az login`, use a "
                "managed identity or set AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET), or set "
                f"FOUNDRY_API_KEY in .env: {(str(exc).splitlines() or [''])[0]}"
            ) from exc

    return token


class _ChatCompletionsModel:
    """Shared Chat Completions logic for the OpenAI API and Azure AI Foundry (same SDK, same wire format)."""

    service = "OpenAI"

    def __init__(self, client: openai.OpenAI, name: str, settings: LLMSettings) -> None:
        self.name = name
        self._client = client
        self._reasoning_effort = settings.reasoning_effort
        self._max_output_tokens = settings.max_output_tokens
        self._json_format = True  # response_format={"type": "json_object"}; turned off if the model rejects it

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        """The model's reply to one system + user prompt."""
        if json_mode and "json" not in f"{system}\n{prompt}".lower():
            system = f"{system}\n\n{JSON_INSTRUCTION}"  # the API insists the word JSON appears in json_object mode
        options: dict[str, Any] = {}
        if self._reasoning_effort:
            options["reasoning_effort"] = self._reasoning_effort
        if self._max_output_tokens:
            options["max_completion_tokens"] = self._max_output_tokens
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        try:
            try:
                if json_mode and self._json_format:
                    options["response_format"] = {"type": "json_object"}
                return self._reply(self._client.chat.completions.create(model=self.name, messages=messages, **options))
            except openai.BadRequestError as exc:
                if "response_format" not in options or not _mentions(exc, "response_format"):
                    raise
                log.warning("%s doesn't support JSON mode; asking for JSON in the prompt instead.", self.name)
                self._json_format = False
                del options["response_format"]
                return self._reply(self._client.chat.completions.create(model=self.name, messages=messages, **options))
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMSetupError(
                f"{self.service} rejected the credentials ({exc.status_code}). {self._auth_hint()}"
            ) from exc
        except openai.NotFoundError as exc:
            raise LLMSetupError(self._not_found_hint()) from exc
        except openai.RateLimitError as exc:
            if exc.code == "insufficient_quota":
                raise LLMSetupError(
                    f"{self.service} says the account has no quota left (insufficient_quota). Check billing."
                ) from exc
            raise LLMUnavailableError(
                f"{self.service} kept throttling requests (429). Lower the batch size or raise your rate limits."
            ) from exc
        except openai.APITimeoutError as exc:
            raise LLMUnavailableError(f"The request to {self.service} timed out.") from exc
        except openai.APIConnectionError as exc:
            raise LLMUnavailableError(
                f"Couldn't connect to {self._client.base_url}. Check your network and {self._endpoint_setting()}."
            ) from exc
        except openai.BadRequestError as exc:
            if exc.code == "content_filter":
                raise LLMError(f"{self.service}'s content filter blocked this text.") from exc
            if exc.code == "context_length_exceeded":
                raise LLMError("The text is too long for the model's context window.") from exc
            if exc.code in _SETUP_ERROR_CODES:
                raise LLMSetupError(
                    f"{self.name} rejected the request settings: {_detail(exc)} "
                    "(check LLM_REASONING_EFFORT and LLM_MAX_OUTPUT_TOKENS)."
                ) from exc
            raise LLMRequestError(f"{self.service} rejected the request: {_detail(exc)}") from exc
        except openai.InternalServerError as exc:
            raise LLMUnavailableError(
                f"{self.service} had a server error ({exc.status_code}); try again later."
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMRequestError(f"{self.service} rejected the request ({exc.status_code}): {_detail(exc)}") from exc

    def _reply(self, response: Any) -> str:
        # OpenAI-compatible gateways sometimes answer 200 with no choices, or with an error object instead.
        choices = getattr(response, "choices", None)
        if not choices or getattr(choices[0], "message", None) is None:
            error = getattr(response, "error", None) or (getattr(response, "model_extra", None) or {}).get("error")
            raise LLMError(f"{self.service} returned no reply" + (f": {error}" if error else " (no choices)."))
        choice = choices[0]
        text = (choice.message.content or "").strip()
        usage = getattr(response, "usage", None)
        if usage is not None:
            log.debug("%s used %s input and %s output tokens.", self.name, usage.prompt_tokens, usage.completion_tokens)
        if choice.finish_reason == "content_filter":
            raise LLMError(f"{self.service}'s content filter blocked the model's reply.")
        refusal = getattr(choice.message, "refusal", None)
        if refusal and not text:
            raise LLMError(f"{self.name} declined to answer: {refusal}")
        if choice.finish_reason == "length":
            if not text:
                raise LLMError("The model ran out of output tokens before replying; raise LLM_MAX_OUTPUT_TOKENS.")
            log.warning("A reply from %s hit the output token limit and may be cut short.", self.name)
        if not text:
            raise LLMError(f"{self.name} returned an empty reply.")
        return text

    def _auth_hint(self) -> str:
        return "Check OPENAI_API_KEY."

    def _not_found_hint(self) -> str:
        gateway = " and OPENAI_BASE_URL" if self._client.base_url.host != "api.openai.com" else ""
        return (
            f'{self.service} has no model named "{self.name}" that this key can use. '
            f"Check LLM_TRIAGE_MODEL / LLM_ANALYSIS_MODEL{gateway}."
        )

    def _endpoint_setting(self) -> str:
        return "OPENAI_BASE_URL"


class OpenAIChatModel(_ChatCompletionsModel):
    """A model on the OpenAI API (or an OpenAI-compatible gateway via OPENAI_BASE_URL)."""

    def __init__(
        self,
        settings: LLMSettings,
        model: str,
        *,
        http_client: httpx2.Client | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        if not model:
            raise ConfigError("No OpenAI model name given; set LLM_TRIAGE_MODEL / LLM_ANALYSIS_MODEL.")
        api_key = settings.openai_api_key
        if not api_key:
            if not settings.openai_base_url:
                raise ConfigError("Set OPENAI_API_KEY in .env to use LLM_PROVIDER=openai.")
            api_key = "not-needed"  # local OpenAI-compatible servers (Ollama, LM Studio, vLLM) ignore the key
        # Always explicit: given None, the SDK reads OPENAI_BASE_URL itself, and a blank one ("OPENAI_BASE_URL=" in
        # .env) would become the base URL "".
        client = openai.OpenAI(
            api_key=api_key,
            base_url=settings.openai_base_url or OPENAI_DEFAULT_BASE_URL,
            max_retries=max_retries,
            http_client=http_client,
        )
        super().__init__(client, model, settings)


class AzureFoundryChatModel(_ChatCompletionsModel):
    """A GPT deployment in Azure AI Foundry (API key, or Entra ID when no key is set)."""

    service = "Foundry"

    def __init__(
        self,
        settings: LLMSettings,
        deployment: str,
        *,
        http_client: httpx2.Client | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        if not settings.foundry_endpoint:
            raise ConfigError("Set FOUNDRY_ENDPOINT in .env to your Foundry resource's endpoint or name.")
        deployment = deployment or settings.foundry_deployment or deployment_from_endpoint(settings.foundry_endpoint)
        if not deployment:
            raise ConfigError("Set FOUNDRY_DEPLOYMENT in .env to the name of your GPT model deployment.")
        client = openai.OpenAI(
            base_url=foundry_base_url(settings.foundry_endpoint),
            # A key if one is set, otherwise Entra ID (az login, managed identity, AZURE_* service principal).
            api_key=settings.foundry_api_key or _entra_token_provider(),
            max_retries=max_retries,  # throttling (429) is common on shared deployments; the SDK honours retry-after
            http_client=http_client,
        )
        super().__init__(client, deployment, settings)

    def _auth_hint(self) -> str:
        return (
            "Check FOUNDRY_API_KEY, or for Entra ID that your identity has the Cognitive Services OpenAI User role "
            "on the resource."
        )

    def _not_found_hint(self) -> str:
        return (
            f'No deployment named "{self.name}" at {self._client.base_url}. Check LLM_TRIAGE_MODEL / '
            "LLM_ANALYSIS_MODEL / FOUNDRY_DEPLOYMENT (the deployment name, not the model name) and FOUNDRY_ENDPOINT."
        )

    def _endpoint_setting(self) -> str:
        return "FOUNDRY_ENDPOINT"


# --- Anthropic -----------------------------------------------------------------------------------------------------


def _import_anthropic() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        raise ConfigError(
            'LLM_PROVIDER=anthropic needs the anthropic package: pip install "news-dip-scanner[anthropic]"'
        ) from exc
    return anthropic


class AnthropicChatModel:
    """A Claude model on the Anthropic API (the anthropic package is the optional "anthropic" extra)."""

    def __init__(
        self,
        settings: LLMSettings,
        model: str,
        *,
        client: Any = None,
        http_client: httpx2.Client | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        if not model:
            raise ConfigError("No Claude model name given; set LLM_TRIAGE_MODEL / LLM_ANALYSIS_MODEL.")
        self.name = model
        self._sdk = _import_anthropic()
        if client is None:
            # Without ANTHROPIC_API_KEY the SDK still finds ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
            # A base URL is only passed when ANTHROPIC_BASE_URL is set but blank: the SDK would use "" as the base
            # URL, while passing one always would override a profile's own base URL.
            blank = os.environ.get("ANTHROPIC_BASE_URL") is not None and not os.environ["ANTHROPIC_BASE_URL"].strip()
            extra = {"base_url": ANTHROPIC_DEFAULT_BASE_URL} if blank else {}
            try:
                client = self._sdk.Anthropic(
                    api_key=settings.anthropic_api_key, max_retries=max_retries, http_client=http_client, **extra
                )
            except self._sdk.CredentialsError as exc:
                raise ConfigError(
                    f"Couldn't load Anthropic credentials ({exc}). Set ANTHROPIC_API_KEY in .env."
                ) from exc
            if not (client.api_key or client.auth_token or getattr(client, "credentials", None)):
                raise ConfigError("Set ANTHROPIC_API_KEY in .env to use LLM_PROVIDER=anthropic.")
        self._client = client
        self._max_tokens = settings.max_output_tokens or ANTHROPIC_MAX_TOKENS
        if self._max_tokens > _ANTHROPIC_NONSTREAMING_MAX_TOKENS:
            log.warning(
                "LLM_MAX_OUTPUT_TOKENS=%d is more than a single Anthropic request can return without streaming; "
                "using %d.",
                self._max_tokens,
                _ANTHROPIC_NONSTREAMING_MAX_TOKENS,
            )
            self._max_tokens = _ANTHROPIC_NONSTREAMING_MAX_TOKENS
        # Some models have a lower limit for requests without streaming; the SDK refuses larger ones.
        limits = getattr(getattr(self._sdk, "_constants", None), "MODEL_NONSTREAMING_TOKENS", None) or {}
        limit = limits.get(model) if isinstance(limits, dict) else None
        if isinstance(limit, int) and 0 < limit < self._max_tokens:
            log.warning("%s allows at most %d output tokens without streaming; using that.", model, limit)
            self._max_tokens = limit
        self._effort = self._pick_effort(settings.reasoning_effort)

    def _pick_effort(self, effort: str | None) -> str | None:
        if not effort:
            return None
        if effort not in _ANTHROPIC_EFFORTS:
            log.warning(
                "LLM_REASONING_EFFORT=%s isn't a Claude effort level (%s); ignoring it for %s.",
                effort,
                ", ".join(_ANTHROPIC_EFFORTS),
                self.name,
            )
            return None
        if "haiku" in self.name:  # Haiku 4.5 rejects the effort parameter
            log.debug("Not sending LLM_REASONING_EFFORT to %s (Haiku doesn't support effort).", self.name)
            return None
        return effort

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        """Claude's reply to one system + user prompt."""
        if json_mode:  # no assistant prefill: current Claude models reject it
            system = f"{system}\n\n{JSON_INSTRUCTION}"
        options: dict[str, Any] = {}
        if self._effort:
            options["output_config"] = {"effort": self._effort}
        sdk = self._sdk
        try:
            response = self._client.messages.create(
                model=self.name,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                **options,
            )
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as exc:
            raise LLMSetupError(
                f"Anthropic rejected the credentials ({exc.status_code}: {_detail(exc)}). Check ANTHROPIC_API_KEY."
            ) from exc
        except sdk.NotFoundError as exc:
            raise LLMSetupError(
                f'Anthropic has no model named "{self.name}" that this key can use. Check LLM_TRIAGE_MODEL / '
                "LLM_ANALYSIS_MODEL (e.g. claude-haiku-4-5, claude-sonnet-5)."
            ) from exc
        except sdk.RateLimitError as exc:
            raise LLMUnavailableError(
                "Anthropic kept throttling requests (429). Lower the batch size or raise your rate limits."
            ) from exc
        except sdk.APITimeoutError as exc:
            raise LLMUnavailableError("The request to Anthropic timed out.") from exc
        except sdk.APIConnectionError as exc:
            raise LLMUnavailableError("Couldn't connect to the Anthropic API. Check your network.") from exc
        except sdk.BadRequestError as exc:
            detail = _detail(exc)
            # This input is too long (checked first: the message also mentions max_tokens).
            if _ANTHROPIC_CONTEXT_OVERFLOW.search(detail):
                raise LLMError("The text is too long for the model's context window.") from exc
            # Spend limits and an empty credit balance come as 400s too; no request can succeed until they're fixed.
            if _ANTHROPIC_ACCOUNT_REFUSAL.search(detail):
                raise LLMSetupError(
                    f"Anthropic refused the request for billing or usage-limit reasons: {detail}"
                ) from exc
            if "effort" in detail or "max_tokens" in detail:
                raise LLMSetupError(
                    f"{self.name} rejected the request settings: {detail} "
                    "(check LLM_REASONING_EFFORT and LLM_MAX_OUTPUT_TOKENS)."
                ) from exc
            raise LLMRequestError(f"Anthropic rejected the request: {detail}") from exc
        except sdk.APIStatusError as exc:
            if exc.status_code == 402 or exc.type == "billing_error":
                raise LLMSetupError(f"Anthropic refused the request for billing reasons: {_detail(exc)}") from exc
            if exc.status_code == 413:
                raise LLMError("The request is too large for the Anthropic API.") from exc
            if exc.status_code >= 500:
                raise LLMUnavailableError(
                    f"Anthropic had a server error or is overloaded ({exc.status_code}); try again later."
                ) from exc
            raise LLMRequestError(f"Anthropic rejected the request ({exc.status_code}): {_detail(exc)}") from exc
        except ValueError as exc:  # the SDK refuses, before sending, requests that would need streaming
            if "streaming is required" not in str(exc).lower():
                raise
            raise LLMSetupError(
                f"{self.name} needs streaming for max_tokens={self._max_tokens}; lower LLM_MAX_OUTPUT_TOKENS or pick "
                "a current model."
            ) from exc
        return self._reply(response)

    def _reply(self, response: Any) -> str:
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
        usage = getattr(response, "usage", None)
        if usage is not None:
            log.debug("%s used %s input and %s output tokens.", self.name, usage.input_tokens, usage.output_tokens)
        stop_reason = response.stop_reason
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            explanation = getattr(details, "explanation", None)
            reason = ": ".join(str(part) for part in (category, explanation) if part)
            raise LLMError(f"{self.name} declined to answer" + (f" ({reason})." if reason else "."))
        if stop_reason == "model_context_window_exceeded":
            if not text:
                raise LLMError("The text is too long for the model's context window (no room left for a reply).")
            log.warning("A reply from %s filled the context window and may be cut short.", self.name)
        if stop_reason == "max_tokens":
            if not text:
                raise LLMError("The model ran out of output tokens before replying; raise LLM_MAX_OUTPUT_TOKENS.")
            log.warning("A reply from %s hit the output token limit and may be cut short.", self.name)
        if not text:
            raise LLMError(f"{self.name} returned an empty reply.")
        return text


# --- helpers -------------------------------------------------------------------------------------------------------


def _detail(exc: Exception) -> str:
    """The API's own error message, without the SDK's "Error code: 400 - {...}" wrapping."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return str(getattr(exc, "message", None) or exc)


def _mentions(exc: Exception, word: str) -> bool:
    return getattr(exc, "param", None) == word or word in _detail(exc)


def build_models(settings: LLMSettings) -> tuple[ChatModel, ChatModel]:
    """The (triage, analysis) models for the configured provider; ConfigError says what's missing."""
    provider = settings.provider
    if provider == "azure":
        if not settings.foundry_endpoint:
            raise ConfigError("Set FOUNDRY_ENDPOINT in .env to your Foundry resource's endpoint or name.")
        fallback = settings.foundry_deployment or deployment_from_endpoint(settings.foundry_endpoint)
        triage_name = settings.triage_model or fallback
        analysis_name = settings.analysis_model or fallback
        if not triage_name or not analysis_name:
            raise ConfigError(
                "Set FOUNDRY_DEPLOYMENT in .env to your GPT deployment's name (used for both steps), or set "
                "LLM_TRIAGE_MODEL and LLM_ANALYSIS_MODEL to two deployment names."
            )
        factory: Callable[[LLMSettings, str], ChatModel] = AzureFoundryChatModel
    elif provider in DEFAULT_MODELS:
        default_triage, default_analysis = DEFAULT_MODELS[provider]
        triage_name = settings.triage_model or default_triage
        analysis_name = settings.analysis_model or default_analysis
        factory = AnthropicChatModel if provider == "anthropic" else OpenAIChatModel
    else:
        raise ConfigError(f"Unknown LLM_PROVIDER {provider!r}; use openai, azure or anthropic.")
    triage_model = factory(settings, triage_name)
    analysis_model = triage_model if analysis_name == triage_name else factory(settings, analysis_name)
    log.debug("Using %s for triage and %s for analysis (%s).", triage_model.name, analysis_model.name, provider)
    return triage_model, analysis_model
