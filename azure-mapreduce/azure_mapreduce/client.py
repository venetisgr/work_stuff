"""Chat deployments in Azure OpenAI / Azure AI Foundry: single calls, async calls and Batch API jobs.

Everything goes through the Azure OpenAI v1 API (``https://<resource>.openai.azure.com/openai/v1/``) with the
standard ``openai`` SDK, authenticated with a key or with Entra ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import openai

from .errors import ConfigError, LLMRequestError, LLMSetupError

log = logging.getLogger(__name__)

Messages = list[dict[str, str]]
AsyncComplete = Callable[[Messages], Awaitable[str]]

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
BATCH_ENDPOINT = "/chat/completions"
_AZURE_HOST_SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
_DEPLOYMENT_IN_URL = re.compile(r"/openai/deployments/([^/?#]+)")
# The request itself is the problem, so sending it again (by any route) fails the same way.
_PERMANENT_CODES = {"content_filter", "ResponsibleAIPolicyViolation", "context_length_exceeded"}
# The deployment rejects the request settings, so every request fails the same way.
_SETUP_CODES = {"unsupported_parameter", "unsupported_value", "OperationNotSupported"}


def foundry_base_url(endpoint: str) -> str:
    """Turn a resource name or an endpoint URL from the Azure portal into the OpenAI v1 base URL."""
    endpoint = endpoint.strip()
    if "://" not in endpoint:  # just the resource name
        return f"https://{endpoint}.openai.azure.com/openai/v1/"
    parsed = urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/openai/v1"):
        return f"{parsed.scheme}://{parsed.netloc}{path}/"
    if (parsed.hostname or "").endswith(_AZURE_HOST_SUFFIXES):
        # Drops paths like /api/projects/<name> or /openai/deployments/<name>/chat/completions.
        return f"{parsed.scheme}://{parsed.netloc}/openai/v1/"
    return f"{parsed.scheme}://{parsed.netloc}{path}/openai/v1/"  # e.g. an API Management gateway


def deployment_from_endpoint(endpoint: str) -> str | None:
    """The deployment name in a pasted Target URI, e.g. .../openai/deployments/<name>/chat/completions?..."""
    match = _DEPLOYMENT_IN_URL.search(endpoint)
    return unquote(match.group(1)) if match else None


@dataclass(frozen=True)
class BatchJob:
    """What the framework needs to know about an Azure batch job."""

    id: str
    status: str  # validating, failed, in_progress, finalizing, completed, expired, cancelling, cancelled
    completed: int = 0
    failed: int = 0
    total: int = 0
    output_file_id: str | None = None
    error_file_id: str | None = None
    errors: tuple[str, ...] = ()

    @classmethod
    def from_sdk(cls, batch: Any) -> BatchJob:
        counts = getattr(batch, "request_counts", None)
        errors = getattr(getattr(batch, "errors", None), "data", None) or []
        return cls(
            id=batch.id,
            status=batch.status,
            completed=getattr(counts, "completed", 0) or 0,
            failed=getattr(counts, "failed", 0) or 0,
            total=getattr(counts, "total", 0) or 0,
            output_file_id=getattr(batch, "output_file_id", None),
            error_file_id=getattr(batch, "error_file_id", None),
            errors=tuple(_describe_batch_error(error) for error in errors),
        )


def _describe_batch_error(error: Any) -> str:
    code = getattr(error, "code", None)
    message = getattr(error, "message", None) or "no details"
    line = getattr(error, "line", None)
    where = f" (line {line})" if line is not None else ""
    return f"{code}: {message}{where}" if code else f"{message}{where}"


class AzureChatClient:
    """A chat model deployment in Azure OpenAI / Azure AI Foundry.

    ``deployment`` is a standard deployment, used for async and one-by-one calls. ``batch_deployment`` is a
    Global Batch or Data Zone Batch deployment, used for Batch API jobs; leave it out to skip the Batch API.

    ``completion_options`` go into every request body, e.g. ``{"temperature": 0, "max_completion_tokens": 800}``
    or ``{"reasoning_effort": "low"}`` for reasoning models.

    Instead of ``endpoint`` and ``api_key`` you can pass a ready-made ``client`` (``openai.OpenAI`` or
    ``openai.AzureOpenAI``) and an ``async_client_factory`` that returns a *new* ``openai.AsyncOpenAI`` or
    ``openai.AsyncAzureOpenAI`` each time it's called (each async run opens and closes its own).
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        deployment: str | None = None,
        batch_deployment: str | None = None,
        api_key: str | None = None,
        completion_options: Mapping[str, Any] | None = None,
        max_retries: int = 6,
        timeout: float = 300.0,
        token_scope: str = FOUNDRY_SCOPE,
        client: openai.OpenAI | None = None,
        async_client_factory: Callable[[], openai.AsyncOpenAI] | None = None,
    ):
        if client is None and not endpoint:
            raise ConfigError("Pass the Azure OpenAI endpoint (or resource name), or a ready-made openai client.")
        self.deployment = deployment or (deployment_from_endpoint(endpoint) if endpoint else None)
        self.batch_deployment = batch_deployment
        if not self.deployment and not self.batch_deployment:
            raise ConfigError("Pass a deployment name (and/or a batch_deployment for the Batch API).")
        self.completion_options = dict(completion_options or {})
        for reserved in ("model", "messages", "stream"):
            if reserved in self.completion_options:
                raise ConfigError(f"completion_options can't set {reserved!r}; the framework fills it in.")
        self._api_key = api_key
        self._max_retries = max_retries
        self._timeout = timeout
        self._token_scope = token_scope
        self._token_provider: Callable[[], str] | None = None
        self._base_url = foundry_base_url(endpoint) if endpoint else None
        self._async_client_factory = async_client_factory
        self._client = client or openai.OpenAI(
            base_url=self._base_url,
            api_key=api_key or self._sync_token_provider(),
            max_retries=max_retries,  # the SDK backs off on 429s and honours retry-after
            timeout=timeout,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> AzureChatClient:
        """Settings from AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_BATCH_DEPLOYMENT and
        AZURE_OPENAI_API_KEY (leave the key unset to sign in with Entra ID). Keyword arguments win."""
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            return env.get(name, "").strip() or None

        settings: dict[str, Any] = {
            "endpoint": get("AZURE_OPENAI_ENDPOINT"),
            "deployment": get("AZURE_OPENAI_DEPLOYMENT"),
            "batch_deployment": get("AZURE_OPENAI_BATCH_DEPLOYMENT"),
            "api_key": get("AZURE_OPENAI_API_KEY"),
        }
        settings.update(overrides)
        if not settings["endpoint"] and settings.get("client") is None:
            raise ConfigError("Set AZURE_OPENAI_ENDPOINT to your resource's endpoint (or its name).")
        return cls(**settings)

    # --- what each strategy can use ----------------------------------------------------------------------

    @property
    def supports_batch(self) -> bool:
        return bool(self.batch_deployment)

    @property
    def supports_async(self) -> bool:
        return bool(self.deployment) and (self._async_client_factory is not None or self._base_url is not None)

    @property
    def supports_sync(self) -> bool:
        return bool(self.deployment)

    def request_body(self, messages: Messages, *, batch: bool = False) -> dict[str, Any]:
        """The chat completions request body for one prompt."""
        model = self.batch_deployment if batch else self.deployment
        return {"model": model, "messages": messages, **self.completion_options}

    # --- one request at a time -----------------------------------------------------------------------------

    def complete(self, messages: Messages) -> str:
        """Send one chat completion request and return the reply text."""
        if not self.deployment:
            raise LLMSetupError("No standard deployment is set, so only the Batch API can be used.")
        try:
            response = self._client.chat.completions.create(**self.request_body(messages))
        except openai.OpenAIError as exc:
            raise translate_error(exc, self.deployment) from exc
        return completion_text(response.model_dump())

    @contextlib.asynccontextmanager
    async def async_session(self) -> AsyncIterator[AsyncComplete]:
        """An async ``complete(messages)`` backed by a fresh async client, closed when the block ends.

        A fresh client per run keeps its connection pool on the event loop that's actually running it.
        """
        if not self.supports_async:
            raise LLMSetupError("No async client is available; pass async_client_factory along with client.")
        client = self._new_async_client()
        deployment = self.deployment

        async def complete(messages: Messages) -> str:
            try:
                response = await client.chat.completions.create(**self.request_body(messages))
            except openai.OpenAIError as exc:
                raise translate_error(exc, deployment) from exc
            return completion_text(response.model_dump())

        try:
            yield complete
        finally:
            await client.close()

    # --- Batch API ---------------------------------------------------------------------------------------

    def batch_line(self, custom_id: str, messages: Messages) -> str:
        """One line of a batch input file."""
        line = {
            "custom_id": custom_id,
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": self.request_body(messages, batch=True),
        }
        return json.dumps(line, ensure_ascii=False)

    def upload_batch_file(self, content: bytes) -> str:
        file = self._client.files.create(file=("requests.jsonl", content, "application/jsonl"), purpose="batch")
        return file.id

    def file_status(self, file_id: str) -> tuple[str | None, str | None]:
        """An uploaded file's processing status (pending, processed, error...) and any details."""
        file = self._client.files.retrieve(file_id)
        return getattr(file, "status", None), getattr(file, "status_details", None)

    def create_batch(self, input_file_id: str) -> BatchJob:
        batch = self._client.batches.create(
            input_file_id=input_file_id,
            endpoint=BATCH_ENDPOINT,  # type: ignore[arg-type]  # Azure takes the path without /v1
            completion_window="24h",
        )
        return BatchJob.from_sdk(batch)

    def get_batch(self, batch_id: str) -> BatchJob:
        return BatchJob.from_sdk(self._client.batches.retrieve(batch_id))

    def cancel_batch(self, batch_id: str) -> BatchJob:
        return BatchJob.from_sdk(self._client.batches.cancel(batch_id))

    def read_file(self, file_id: str) -> str:
        return self._client.files.content(file_id).text

    def delete_file(self, file_id: str) -> None:
        self._client.files.delete(file_id)

    # --- authentication ----------------------------------------------------------------------------------

    def _sync_token_provider(self) -> Callable[[], str]:
        if self._token_provider is None:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            # az login, a managed identity, or a service principal from AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET.
            self._token_provider = get_bearer_token_provider(DefaultAzureCredential(), self._token_scope)
        return self._token_provider

    def _new_async_client(self) -> openai.AsyncOpenAI:
        if self._async_client_factory is not None:
            return self._async_client_factory()
        api_key: str | Callable[[], Awaitable[str]]
        if self._api_key:
            api_key = self._api_key
        else:
            provider = self._sync_token_provider()

            async def api_key() -> str:  # the credential caches tokens, so this rarely does real work
                return await asyncio.to_thread(provider)

        return openai.AsyncOpenAI(
            base_url=self._base_url, api_key=api_key, max_retries=self._max_retries, timeout=self._timeout
        )


def completion_text(body: Mapping[str, Any]) -> str:
    """The reply text from a chat completion response body, or an LLMRequestError saying why there isn't one."""
    choices = body.get("choices") or []
    if not choices:
        raise LLMRequestError("The response had no choices.", retryable=True)
    choice = choices[0]
    message = choice.get("message") or {}
    text = (message.get("content") or "").strip()
    finish_reason = choice.get("finish_reason")
    if finish_reason == "content_filter":
        raise LLMRequestError("Azure's content filter blocked the reply.", retryable=False, code="content_filter")
    if not text and message.get("refusal"):
        raise LLMRequestError(f"The model refused: {message['refusal']}", retryable=False, code="refusal")
    if finish_reason == "length":
        if not text:
            raise LLMRequestError(
                "The model ran out of output tokens before replying; raise max_completion_tokens.",
                retryable=False,
                code="length",
            )
        log.warning("A reply hit the output token limit and may be cut short; raise max_completion_tokens.")
    if not text:
        raise LLMRequestError("The model returned an empty reply.", retryable=True, code="empty")
    return text


def translate_error(exc: openai.OpenAIError, deployment: str | None) -> LLMRequestError | LLMSetupError:
    """Map an SDK exception to "this request failed" or "every request will fail"."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return LLMSetupError(
            f"Azure rejected the credentials ({exc.status_code}). Check the API key, or for Entra ID that your "
            "identity has the Cognitive Services OpenAI User role on the resource."
        )
    if isinstance(exc, openai.NotFoundError):
        return LLMSetupError(
            f'No deployment named "{deployment}" at this endpoint ({message}). Use the deployment name, '
            "not the model name, and check the endpoint."
        )
    if isinstance(exc, openai.APITimeoutError):  # a subclass of APIConnectionError, so check it first
        return LLMRequestError("The request timed out.", retryable=True, code="timeout")
    if isinstance(exc, openai.APIConnectionError):
        return LLMSetupError(f"Couldn't connect to Azure OpenAI ({message}). Check the endpoint and network.")
    if isinstance(exc, openai.RateLimitError):
        return LLMRequestError(
            "Azure kept throttling requests (429); lower max_concurrency or raise the deployment's quota.",
            retryable=True,
            code="rate_limit",
        )
    if isinstance(exc, openai.BadRequestError):
        if code in _SETUP_CODES:
            return LLMSetupError(f"The deployment rejected the request settings: {message}")
        if code in _PERMANENT_CODES:
            return LLMRequestError(_permanent_message(code, message), retryable=False, code=code)
        return LLMRequestError(f"Azure rejected the request: {message}", retryable=False, code=code)
    if isinstance(exc, openai.UnprocessableEntityError):
        return LLMRequestError(f"Azure rejected the request: {message}", retryable=False, code=code)
    return LLMRequestError(f"The request failed: {message}", retryable=True, code=code)


def batch_line_result(line: Mapping[str, Any]) -> str | LLMRequestError:
    """The reply text, or the error, for one line of a batch output or error file."""
    error = line.get("error")
    if error:
        code = error.get("code")
        message = error.get("message") or "no details"
        return LLMRequestError(_permanent_message(code, message), retryable=code not in _PERMANENT_CODES, code=code)
    response = line.get("response") or {}
    body = response.get("body") or {}
    if isinstance(body, str):  # some gateways return the body as a JSON string
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    status = response.get("status_code")
    if status != 200:
        detail = body.get("error") or {} if isinstance(body, dict) else {}
        code = detail.get("code")
        message = detail.get("message") or f"status {status}"
        # A batch deployment can fail where the standard deployment works (and vice versa), so only errors
        # about the request's own content are final.
        return LLMRequestError(_permanent_message(code, message), retryable=code not in _PERMANENT_CODES, code=code)
    try:
        return completion_text(body)
    except LLMRequestError as exc:
        return exc


def _permanent_message(code: str | None, message: str) -> str:
    if code in ("content_filter", "ResponsibleAIPolicyViolation"):
        return f"Azure's content filter blocked this text: {message}"
    if code == "context_length_exceeded":
        return f"The text is too long for the model: {message}"
    return f"{code}: {message}" if code else message
