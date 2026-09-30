"""Chat deployments in Azure OpenAI / Azure AI Foundry: single calls, async calls and Batch API jobs.

Everything goes through the Azure OpenAI v1 API (``https://<resource>.openai.azure.com/openai/v1/``) with the
standard ``openai`` SDK, authenticated with a key or with Entra ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NoReturn
from urllib.parse import unquote, urlparse

import openai

from .errors import ConfigError, LLMRequestError, LLMSetupError

log = logging.getLogger(__name__)

Messages = list[dict[str, str]]
AsyncComplete = Callable[[Messages], Awaitable[str]]

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
# The Batch API endpoint, used both as each input line's "url" and as the job's endpoint (as in the v1 API spec).
BATCH_ENDPOINT = "/v1/chat/completions"
# Uploaded input files and generated output files expire after this long (14 days, Azure's minimum), so files
# left behind by an interrupted run don't pile up against the resource's file limit.
BATCH_FILE_EXPIRY_SECONDS = 14 * 24 * 3600
_AZURE_HOST_SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
_DEPLOYMENT_IN_URL = re.compile(r"/openai/deployments/([^/?#]+)")
# The request itself is the problem, so sending it again (by any route) fails the same way.
_PERMANENT_CODES = {"content_filter", "ResponsibleAIPolicyViolation", "context_length_exceeded"}
# The deployment rejects the request settings, so every request fails the same way.
_SETUP_CODES = {"unsupported_parameter", "unsupported_value", "OperationNotSupported"}
QUOTA_CODE = "token_limit_exceeded"  # the Batch API's enqueued-token quota is full for now
# Batch validation errors that every job would hit again (wrong deployment, not a batch deployment...).
BATCH_SETUP_CODES = {
    "model_not_found",
    "model_mismatch",
    "invalid_request",
    "url_mismatch",
    "invalid_json_line",
    "empty_file",
    "duplicate_custom_id",
    "too_many_tasks",
    "DeploymentNotFound",
    "OperationNotSupported",
    "unsupported_parameter",
    "unsupported_value",
}
# The classic, api-version based API (openai.AzureOpenAI) documents the unprefixed path.
LEGACY_BATCH_ENDPOINT = "/chat/completions"
_EXPIRY_RANGE = (14 * 24 * 3600, 30 * 24 * 3600)  # what Azure accepts for batch file expiry
# Options the SDK uses to shape the HTTP call rather than the request body, so they can't go in a batch file.
_SDK_ONLY_OPTIONS = {"extra_body", "extra_headers", "extra_query", "timeout"}


def foundry_base_url(endpoint: str) -> str:
    """Turn a resource name or an endpoint URL from the Azure portal into the OpenAI v1 base URL."""
    endpoint = endpoint.strip()
    if "://" not in endpoint:
        if "." not in endpoint and "/" not in endpoint:  # just the resource name
            return f"https://{endpoint}.openai.azure.com/openai/v1/"
        endpoint = f"https://{endpoint}"  # a host name pasted without https://
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
    errors: tuple[str, ...] = ()  # why the job failed validation, as "code: message"
    error_codes: tuple[str, ...] = ()

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
            error_codes=tuple(code for error in errors if (code := getattr(error, "code", None))),
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

    ``batch_endpoint`` is the path written into Batch API input files and jobs: the v1 API's
    ``/v1/chat/completions``, or ``/chat/completions`` for a legacy ``openai.AzureOpenAI`` client.
    ``batch_file_expiry`` is how many seconds uploaded and generated batch files are kept, from 14 to 30 days
    (None: until deleted; by default 14 days on the v1 API and not set for a legacy client, whose older API
    versions don't take it).
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
        batch_endpoint: str | None = None,
        batch_file_expiry: int | None | Literal["auto"] = "auto",
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
        for sdk_only in sorted(_SDK_ONLY_OPTIONS & set(self.completion_options)):
            raise ConfigError(
                f"completion_options can't set {sdk_only!r}, which isn't part of the request body (and so can't go "
                "in a batch file). Put body fields straight into completion_options; unknown ones are passed on."
            )
        legacy = _is_classic_azure_client(client)
        self.batch_endpoint = batch_endpoint or (LEGACY_BATCH_ENDPOINT if legacy else BATCH_ENDPOINT)
        if batch_file_expiry == "auto":
            batch_file_expiry = None if legacy else BATCH_FILE_EXPIRY_SECONDS
        if batch_file_expiry is not None and (
            not isinstance(batch_file_expiry, int)
            or isinstance(batch_file_expiry, bool)
            or not _EXPIRY_RANGE[0] <= batch_file_expiry <= _EXPIRY_RANGE[1]
        ):
            raise ConfigError(
                f"batch_file_expiry must be None or a whole number of seconds from {_EXPIRY_RANGE[0]} (14 days) to "
                f"{_EXPIRY_RANGE[1]} (30 days), the range Azure accepts (got {batch_file_expiry!r})."
            )
        self.batch_file_expiry = batch_file_expiry
        self._api_key = api_key
        self._max_retries = max_retries
        self._timeout = timeout
        self._token_scope = token_scope
        self._token_provider: Callable[[], str] | None = None
        self._base_url = foundry_base_url(endpoint) if endpoint else None
        self._async_client_factory = async_client_factory
        if client is not None and async_client_factory is None:
            log.warning(
                "No async_client_factory was given with the client, so the async strategy is skipped; pass one "
                "that returns a new openai.AsyncOpenAI (or AsyncAzureOpenAI) to use it."
            )
        self._client = client or openai.OpenAI(
            base_url=self._base_url,
            api_key=api_key or self._sync_token_provider(),
            max_retries=max_retries,  # the SDK backs off on 429s and honours retry-after
            timeout=_timeouts(timeout),
        )
        self._chat_parameters = _parameter_names(self._client.chat.completions.create)

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
        arguments = _split_arguments(self.request_body(messages), self._chat_parameters)
        try:
            response = self._client.chat.completions.create(**arguments)
        except Exception as exc:
            _raise_translated(exc, self.deployment)
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
        parameters = _parameter_names(client.chat.completions.create)

        async def complete(messages: Messages) -> str:
            arguments = _split_arguments(self.request_body(messages), parameters)
            try:
                response = await client.chat.completions.create(**arguments)
            except Exception as exc:
                _raise_translated(exc, deployment)
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
            "url": self.batch_endpoint,
            "body": self.request_body(messages, batch=True),
        }
        return json.dumps(line)  # ASCII only: no raw line separators or lone surrogates in the file

    def upload_batch_file(self, content: bytes) -> str:
        options: dict[str, Any] = {}
        if self.batch_file_expiry is not None:
            options["expires_after"] = {"anchor": "created_at", "seconds": self.batch_file_expiry}
        file = self._batch_api(
            self._client.files.create,
            file=("requests.jsonl", content, "application/jsonl"),
            purpose="batch",
            **options,
        )
        return file.id

    def file_status(self, file_id: str) -> tuple[str | None, str | None]:
        """An uploaded file's processing status (pending, processed, error...) and any details."""
        file = self._batch_api(self._client.files.retrieve, file_id)
        return getattr(file, "status", None), getattr(file, "status_details", None)

    def create_batch(self, input_file_id: str) -> BatchJob:
        """Start a batch job on an uploaded input file.

        Creating a job isn't idempotent, so the SDK doesn't retry it (a retry after a lost response would start
        a second, billed job). After a failure that may not have reached Azure, a job already running on this
        input file is adopted instead of failing; the batch runner retries the rest.
        """
        options: dict[str, Any] = {}
        if self.batch_file_expiry is not None:
            options["output_expires_after"] = {"anchor": "created_at", "seconds": self.batch_file_expiry}
        try:
            batch = self._batch_api(
                _without_retries(self._client).batches.create,
                input_file_id=input_file_id,
                endpoint=self.batch_endpoint,
                completion_window="24h",
                **options,
            )
        except LLMRequestError as exc:
            if not exc.retryable or (existing := self._find_batch(input_file_id)) is None:
                raise
            log.info("Creating the batch job failed (%s), but Azure had started it; carrying on with it.", exc)
            return existing
        return BatchJob.from_sdk(batch)

    def _find_batch(self, input_file_id: str) -> BatchJob | None:
        """A recent batch job on this input file, if there is one."""
        try:
            page = self._client.batches.list(limit=50)
            for batch in getattr(page, "data", None) or []:
                if getattr(batch, "input_file_id", None) == input_file_id:
                    return BatchJob.from_sdk(batch)
        except Exception as exc:
            log.debug("Couldn't list batch jobs (%s).", exc)
        return None

    def get_batch(self, batch_id: str) -> BatchJob:
        return BatchJob.from_sdk(self._batch_api(self._client.batches.retrieve, batch_id))

    def cancel_batch(self, batch_id: str) -> BatchJob:
        return BatchJob.from_sdk(self._batch_api(self._client.batches.cancel, batch_id))

    def read_file(self, file_id: str) -> str:
        return self._batch_api(self._client.files.content, file_id).text

    def delete_file(self, file_id: str) -> None:
        self._batch_api(self._client.files.delete, file_id)

    def _batch_api(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            _raise_translated(exc, self.batch_deployment, batch=True)

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
            base_url=self._base_url, api_key=api_key, max_retries=self._max_retries, timeout=_timeouts(self._timeout)
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


def translate_error(
    exc: BaseException, deployment: str | None, *, batch: bool = False
) -> LLMRequestError | LLMSetupError | None:
    """Map an SDK or sign-in exception to "this request failed" or "every request will fail".

    Returns None for exceptions it doesn't know, which the caller re-raises as they are.
    """
    if isinstance(exc, UnicodeError):  # e.g. a lone surrogate the request can't be encoded with
        return LLMRequestError(f"The text can't be sent as UTF-8: {exc}", retryable=False, code="encoding")
    if not isinstance(exc, openai.OpenAIError):
        return _translate_credential_error(exc)
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if batch and code is None:
        codes = _wrapped_error_codes(getattr(exc, "body", None))
        code = QUOTA_CODE if QUOTA_CODE in codes else next(iter(sorted(codes & BATCH_SETUP_CODES)), None)
        if code in BATCH_SETUP_CODES and isinstance(exc, openai.BadRequestError):
            return LLMSetupError(f"Azure rejected the batch job, and would reject every one: {message}")
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        role = (
            "Cognitive Services OpenAI Contributor role (the Batch API uploads files and creates jobs, which the "
            "OpenAI User role can't)"
            if batch
            else "Cognitive Services OpenAI User role"
        )
        return LLMSetupError(
            f"Azure rejected the credentials ({exc.status_code}: {message}). Check the API key, or for Entra ID "
            f"that your identity has the {role} on the resource."
        )
    if isinstance(exc, openai.NotFoundError):
        if batch:
            return LLMSetupError(
                f"The Batch API call was answered with 404 ({message}). Check that the endpoint's resource offers "
                "the Batch API and that the batch deployment exists."
            )
        return LLMSetupError(
            f'No deployment named "{deployment}" at this endpoint ({message}). Use the deployment name, '
            "not the model name, and check the endpoint."
        )
    if isinstance(exc, openai.APITimeoutError):  # a subclass of APIConnectionError, so check it first
        if type(exc.__cause__).__name__ in ("ConnectTimeout", "PoolTimeout"):
            # No connection at all (a firewall dropping packets, say): the runners count these as unreachable.
            return LLMRequestError(
                "Couldn't connect to Azure OpenAI (the connection timed out). Check the endpoint and the network.",
                retryable=True,
                code="connection",
            )
        return LLMRequestError("The request timed out.", retryable=True, code="timeout")
    if isinstance(exc, openai.APIConnectionError):
        # Only a failure on request after request means the endpoint is wrong; the runners count them.
        return LLMRequestError(
            f"Couldn't connect to Azure OpenAI ({message}). Check the endpoint and the network.",
            retryable=True,
            code="connection",
        )
    if isinstance(exc, openai.RateLimitError):
        return LLMRequestError(
            "Azure kept throttling requests (429); lower max_concurrency or raise the deployment's quota.",
            retryable=True,
            code="rate_limit",
        )
    if isinstance(exc, openai.BadRequestError):
        if code == QUOTA_CODE or QUOTA_CODE in message:
            return LLMRequestError(
                f"The Batch API's enqueued-token quota is full: {message}", retryable=True, code=QUOTA_CODE
            )
        if code in _SETUP_CODES:
            return LLMSetupError(f"The deployment rejected the request settings: {message}")
        if code in _PERMANENT_CODES:
            return LLMRequestError(_permanent_message(code, message), retryable=False, code=code)
        return LLMRequestError(f"Azure rejected the request: {message}", retryable=False, code=code)
    if isinstance(exc, openai.UnprocessableEntityError):
        return LLMRequestError(f"Azure rejected the request: {message}", retryable=False, code=code)
    return LLMRequestError(f"The request failed: {message}", retryable=True, code=code)


def _wrapped_error_codes(body: object) -> set[str]:
    """Error codes from a body shaped {"errors": [...]} or {"errors": {"data": [...]}}, as batch creation replies."""
    errors = body.get("errors") if isinstance(body, Mapping) else None
    if isinstance(errors, Mapping):
        errors = errors.get("data")
    if not isinstance(errors, list):
        return set()
    return {error["code"] for error in errors if isinstance(error, Mapping) and isinstance(error.get("code"), str)}


def _translate_credential_error(exc: BaseException) -> LLMRequestError | LLMSetupError | None:
    """Entra ID sign-in failures raised by azure-identity while the SDK fetches a token.

    DefaultAzureCredential raises ClientAuthenticationError("DefaultAzureCredential failed to retrieve a token
    ...") when no credential in its chain works: a setup problem. Once one has worked it calls that one
    directly, and a managed identity endpoint that doesn't answer raises CredentialUnavailableError: a passing
    problem. The runners stop on a run of passing problems anyway.
    """
    try:
        from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError, ServiceResponseError
    except ImportError:  # pragma: no cover - azure-identity is a dependency
        return None
    if isinstance(exc, ClientAuthenticationError) and "DefaultAzureCredential failed to retrieve a token" in str(exc):
        return LLMSetupError(
            f"No Entra ID credential worked ({exc}). Pass an API key, run `az login`, or set AZURE_TENANT_ID, "
            "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET."
        )
    if isinstance(exc, ClientAuthenticationError | ServiceRequestError | ServiceResponseError):
        return LLMRequestError(f"Couldn't get an Entra ID token: {exc}", retryable=True, code="credential")
    return None


def _is_classic_azure_client(client: object) -> bool:
    """An openai.AzureOpenAI client on the classic, api-version based API (not one pointed at /openai/v1/)."""
    if not isinstance(client, openai.AzureOpenAI):
        return False
    return not str(getattr(client, "base_url", "")).rstrip("/").endswith("/openai/v1")


def _timeouts(total: float) -> Any:
    """The request timeout, with a short connect timeout so an endpoint that drops packets is noticed quickly."""
    try:
        import httpx2
    except ImportError:  # pragma: no cover - the openai SDK's HTTP library
        return total
    return httpx2.Timeout(total, connect=min(15.0, total))


def _without_retries(client: Any) -> Any:
    """The same client with the SDK's automatic retries off (for calls that aren't safe to repeat)."""
    with_options = getattr(client, "with_options", None)
    return with_options(max_retries=0) if callable(with_options) else client


def _raise_translated(exc: Exception, deployment: str | None, *, batch: bool = False) -> NoReturn:
    translated = translate_error(exc, deployment, batch=batch)
    if translated is None:
        raise exc
    raise translated from exc


def _parameter_names(method: Callable[..., Any]) -> frozenset[str] | None:
    """The keyword arguments an SDK method takes by name (None if it can't be inspected)."""
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return None
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return None  # takes anything (a wrapper or a test double), so pass everything by name
    return frozenset(parameters)


def _split_arguments(body: Mapping[str, Any], parameters: frozenset[str] | None) -> dict[str, Any]:
    """Keyword arguments for chat.completions.create: settings the SDK doesn't name go in extra_body."""
    if parameters is None:
        return dict(body)
    arguments = {key: value for key, value in body.items() if key in parameters}
    extra = {key: value for key, value in body.items() if key not in parameters}
    if extra:
        arguments["extra_body"] = extra
    return arguments


def batch_line_result(line: object) -> str | LLMRequestError:
    """The reply text, or the error, for one line of a batch output or error file."""
    if not isinstance(line, Mapping):
        return LLMRequestError("The batch result line isn't a JSON object.", retryable=True, code="unreadable")
    error = line.get("error")
    if error:
        code, message = _error_details(error)
        return LLMRequestError(_permanent_message(code, message), retryable=code not in _PERMANENT_CODES, code=code)
    response = line.get("response") or {}
    if not isinstance(response, Mapping):
        return LLMRequestError("The batch result line has no readable response.", retryable=True, code="unreadable")
    body = response.get("body") or {}
    if isinstance(body, str):  # some gateways return the body as a JSON string
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    if not isinstance(body, Mapping):
        body = {}
    status = response.get("status_code")
    if status != 200:
        code, message = _error_details(body.get("error") or f"status {status}")
        # A batch deployment can fail where the standard deployment works (and vice versa), so only errors
        # about the request's own content are final.
        return LLMRequestError(_permanent_message(code, message), retryable=code not in _PERMANENT_CODES, code=code)
    try:
        return completion_text(body)
    except LLMRequestError as exc:
        return exc
    except (AttributeError, TypeError, IndexError, KeyError):
        return LLMRequestError("The batch reply isn't a chat completion.", retryable=True, code="unreadable")


def _error_details(error: object) -> tuple[str | None, str]:
    """The code and message of an error object, unwrapping {"code": null, "message": {"error": {...}}}."""
    if isinstance(error, Mapping):
        code, message = error.get("code"), error.get("message")
        if code is None and isinstance(message, str) and message.lstrip().startswith("{"):
            with contextlib.suppress(ValueError):
                message = json.loads(message)
        if code is None and isinstance(message, Mapping):
            inner = message.get("error")
            if isinstance(inner, Mapping):
                return _error_details(inner)
            if isinstance(inner, str) and inner:
                return None, inner
            if message.get("code") is not None:
                return _error_details(message)
        if isinstance(message, Mapping | list):
            message = json.dumps(message, default=str)  # keep Azure's text rather than a Python repr
        return (str(code) if code is not None else None), str(message or "no details")
    return None, str(error)


def _permanent_message(code: str | None, message: str) -> str:
    if code in ("content_filter", "ResponsibleAIPolicyViolation"):
        return f"Azure's content filter blocked this text: {message}"
    if code == "context_length_exceeded":
        return f"The text is too long for the model: {message}"
    return f"{code}: {message}" if code else message
