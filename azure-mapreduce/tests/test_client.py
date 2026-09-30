"""AzureChatClient and its helpers against the real openai SDK, with a fake Azure OpenAI v1 service behind
httpx2.MockTransport; ends with map-reduce runs that go through the Batch API and its fallbacks end to end."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import default as email_policy
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pandas as pd
import pytest
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import CredentialUnavailableError

from azure_mapreduce import MapReduce
from azure_mapreduce.client import (
    BATCH_ENDPOINT,
    BATCH_FILE_EXPIRY_SECONDS,
    BATCH_SETUP_CODES,
    FOUNDRY_SCOPE,
    LEGACY_BATCH_ENDPOINT,
    QUOTA_CODE,
    AzureChatClient,
    BatchJob,
    batch_line_result,
    completion_text,
    deployment_from_endpoint,
    foundry_base_url,
    translate_error,
)
from azure_mapreduce.errors import ConfigError, LLMRequestError, LLMSetupError

from .conftest import chat_body

# The real SDK classes, kept before any test swaps them out on the openai module.
RealOpenAI = openai.OpenAI
RealAsyncOpenAI = openai.AsyncOpenAI
RealAzureOpenAI = openai.AzureOpenAI

BASE_URL = "https://res.openai.azure.com/openai/v1/"
MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hello"}]

# --- helpers ---------------------------------------------------------------------------------------------


def answer(prompt: str) -> str:
    """The fake model: S(x) for a map prompt, C(a+b+...) for a reduce prompt over the texts it joins."""
    if prompt.startswith("Summarize: "):
        return f"S({prompt.removeprefix('Summarize: ')})"
    if prompt.startswith("Combine: "):
        return "C(" + "+".join(prompt.removeprefix("Combine: ").split("\n\n")) + ")"
    return f"<{prompt}>"


def error_json(code: str | None, message: str = "nope") -> dict:
    return {"error": {"code": code, "message": message, "param": None, "type": None}}


def multipart(request: httpx2.Request) -> dict[str, Any]:
    """The parts of a multipart/form-data request, by field name."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=email_policy).parsebytes(head + request.content)
    return {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}


def sdk_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> openai.OpenAI:
    transport = httpx2.MockTransport(handler)
    return RealOpenAI(base_url=BASE_URL, api_key="k", max_retries=0, http_client=httpx2.Client(transport=transport))


def async_sdk_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> openai.AsyncOpenAI:
    transport = httpx2.MockTransport(handler)
    return RealAsyncOpenAI(
        base_url=BASE_URL, api_key="k", max_retries=0, http_client=httpx2.AsyncClient(transport=transport)
    )


def legacy_sdk_client(handler: Callable[[httpx2.Request], httpx2.Response], max_retries: int = 0) -> openai.AzureOpenAI:
    """A classic, api-version based openai.AzureOpenAI client."""
    return RealAzureOpenAI(
        azure_endpoint="https://res.openai.azure.com",
        api_key="secret",
        api_version="2024-10-21",
        max_retries=max_retries,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def client_on(handler, async_handler=None, **options) -> AzureChatClient:
    """An AzureChatClient whose sync and async SDK clients send every request to ``handler``."""
    options = {"deployment": "gpt-test", "batch_deployment": "gpt-batch", **options}
    return AzureChatClient(
        client=sdk_client(handler),
        async_client_factory=lambda: async_sdk_client(async_handler or handler),
        **options,
    )


def responding(status: int, body: Any = None, seen: list | None = None):
    """A handler that answers every request with ``status`` and a JSON ``body`` (and records the requests)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body if body is not None else chat_body("ok"))

    return handler


def raising(error: type[Exception]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error("boom", request=request)

    return handler


async def ask_async(client: AzureChatClient, messages=MESSAGES) -> str:
    async with client.async_session() as complete:
        return await complete(messages)


# DefaultAzureCredential's error when no credential in its chain works (azure-identity 1.25, out of the box).
NO_CREDENTIAL_WORKED = (
    "DefaultAzureCredential failed to retrieve a token from the included credentials.\n"
    "Attempted credentials:\n"
    "\tEnvironmentCredential: EnvironmentCredential authentication unavailable. Environment variables are not "
    "fully configured.\n"
    "Visit https://aka.ms/azsdk/python/identity/environmentcredential/troubleshoot to troubleshoot this issue.\n"
    "\tManagedIdentityCredential: ManagedIdentityCredential authentication unavailable, no response from the IMDS "
    "endpoint.\n"
    "\tAzureCliCredential: Azure CLI not found on path\n"
    "To mitigate this issue, please refer to the troubleshooting guidelines here at "
    "https://aka.ms/azsdk/python/identity/defaultazurecredential/troubleshoot."
)


def no_credential_worked() -> Exception:
    return ClientAuthenticationError(message=NO_CREDENTIAL_WORKED)


def credential_unavailable() -> Exception:
    return CredentialUnavailableError("EnvironmentCredential authentication unavailable. No credential found.")


def client_authentication_failed() -> Exception:
    return ClientAuthenticationError("ManagedIdentityCredential: the token request timed out")


def service_request_error() -> Exception:
    return ServiceRequestError("Couldn't reach the managed identity endpoint: connection refused")


def service_response_error() -> Exception:
    return ServiceResponseError("The managed identity endpoint closed the connection")


class FakeAzure:
    """A fake Azure OpenAI v1 service (chat completions, files and batch jobs) behind httpx2.MockTransport.

    - Chat completions answer the last message with ``reply``.
    - An uploaded file reports ``file_statuses`` on successive retrieves (the last one repeats).
    - A batch job reports ``batch_statuses`` on successive retrieves (the last one repeats), or ``cancelled`` once
      cancelled. Once it's completed (expired or cancelled: only the first half of its requests) its output and
      error files are built from the uploaded JSONL,
      answering each line with ``reply``, except prompts in ``line_errors`` (``prompt -> (status, code)``), which
      go to the error file. Output lines come back in reverse order: Azure doesn't promise input order.
    - ``fail["<METHOD> <route>"] = (status, code)`` fails those requests; ids in the route read ``{id}`` (e.g.
      ``"POST /batches"``, ``"GET /files/{id}"``). Prefix with ``"sync "`` or ``"async "`` to fail only that
      client's requests, or use ``"*"`` to fail everything. ``fail_times[key] = n`` limits the ``fail`` entry
      ``key`` to the first n requests it matches (the route works after that). Failures carry
      ``retry-after-ms: 1``, so an SDK client that does retry them doesn't slow the tests down.
    - ``lose[key] = n`` handles the first n requests matching ``key`` (as in ``fail``) but loses the response:
      the transport raises httpx2.ReadTimeout, or with ``lose_as`` set, answers with that status. E.g. a job
      Azure created although the client never heard back.
    - ``disconnect`` holds routes (keys as in ``fail``) whose connection drops: the transport raises
      httpx2.ConnectError, as when Azure can't be reached. ``drop_prompts[prompt] = n`` drops the connection of
      the first n chat requests for that prompt.
    - ``GET /batches?limit=n`` lists the newest n jobs first.
    - As on Azure, the Batch API only takes ``batch_url`` (the v1 API's ``/v1/chat/completions``): a job whose
      endpoint or input lines name another URL fails validation with ``url_mismatch``, one error per line.
    - The classic API's paths (``/openai/files?api-version=...``, from an openai.AzureOpenAI client) are served
      too, under the same routes.
    """

    def __init__(
        self,
        *,
        reply: Callable[[str], str] = answer,
        file_statuses: tuple[str, ...] = ("pending", "processed"),
        status_details: str | None = None,
        batch_statuses: tuple[str, ...] = ("in_progress", "completed"),
        batch_errors: tuple[dict, ...] = (),
        line_errors: dict[str, tuple[int, str | None]] | None = None,
        fail: dict[str, tuple[int, str | None]] | None = None,
        fail_times: dict[str, int] | None = None,
        disconnect: set[str] | frozenset[str] = frozenset(),
        drop_prompts: dict[str, int] | None = None,
        batch_url: str = "/v1/chat/completions",
        lose: dict[str, int] | None = None,
        lose_as: int | None = None,
    ):
        self.lose = dict(lose or {})
        self.lose_as = lose_as
        self.reply = reply
        self.file_statuses = file_statuses
        self.status_details = status_details
        self.batch_statuses = batch_statuses
        self.batch_errors = batch_errors
        self.line_errors = line_errors or {}
        self.fail = fail or {}
        self.fail_times = dict(fail_times or {})
        self.disconnect = set(disconnect)
        self.drop_prompts = dict(drop_prompts or {})
        self.batch_url = batch_url
        self.files: dict[str, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, httpx2.Request]] = []  # (which client, request)
        self.chat_calls: list[tuple[str, str, str]] = []  # (which client, model, prompt)
        self.chat_bodies: list[dict] = []
        self.batch_lines: list[dict] = []  # every line of every input file a batch job ran
        self.deleted: list[str] = []
        self.async_clients: list[openai.AsyncOpenAI] = []

    # --- clients -----------------------------------------------------------------------------------------

    def transport(self, tag: str) -> httpx2.MockTransport:
        return httpx2.MockTransport(lambda request: self.handle(tag, request))

    def sdk_client(self) -> openai.OpenAI:
        http = httpx2.Client(transport=self.transport("sync"))
        return RealOpenAI(base_url=BASE_URL, api_key="k", max_retries=0, http_client=http)

    def async_sdk_client(self) -> openai.AsyncOpenAI:
        http = httpx2.AsyncClient(transport=self.transport("async"))
        client = RealAsyncOpenAI(base_url=BASE_URL, api_key="k", max_retries=0, http_client=http)
        self.async_clients.append(client)
        return client

    def client(self, **options: Any) -> AzureChatClient:
        options = {"deployment": "gpt-test", "batch_deployment": "gpt-batch", **options}
        return AzureChatClient(client=self.sdk_client(), async_client_factory=self.async_sdk_client, **options)

    def legacy_sdk_client(self) -> openai.AzureOpenAI:
        """A classic openai.AzureOpenAI client (api-version paths, api-key header) on this service."""
        return RealAzureOpenAI(
            azure_endpoint="https://res.openai.azure.com",
            api_key="secret",
            api_version="2024-10-21",
            max_retries=0,
            http_client=httpx2.Client(transport=self.transport("sync")),
        )

    # --- inspection --------------------------------------------------------------------------------------

    def sent(self, method: str, route: str) -> list[httpx2.Request]:
        return [request for _, request in self.requests if request.method == method and _route(request) == route]

    def add_file(
        self,
        content: bytes,
        *,
        filename: str = "output.jsonl",
        purpose: str = "batch_output",
        expires_after: dict | None = None,
    ) -> str:
        file_id = f"file-{len(self.files) + 1}"
        self.files[file_id] = {
            "content": content,
            "filename": filename,
            "purpose": purpose,
            "retrieves": 0,
            "expires_after": expires_after,
        }
        return file_id

    def uploads(self) -> list[str]:
        """The ids of the batch input files that were uploaded."""
        return [file_id for file_id, file in self.files.items() if file["purpose"] == "batch"]

    # --- the service -------------------------------------------------------------------------------------

    def handle(self, tag: str, request: httpx2.Request) -> httpx2.Response:
        response = self._handle(tag, request)
        key = f"{request.method} {_route(request)}"
        losing = next((name for name in (f"{tag} {key}", key, "*") if self.lose.get(name, 0) > 0), None)
        if losing is not None:  # handled, but the answer never reaches the client
            self.lose[losing] -= 1
            if self.lose_as is None:
                raise httpx2.ReadTimeout("The read operation timed out", request=request)
            return httpx2.Response(self.lose_as, json=error_json("server_error"), headers={"retry-after-ms": "1"})
        return response

    def _handle(self, tag: str, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((tag, request))
        route = _route(request)
        key = f"{request.method} {route}"
        names = (f"{tag} {key}", key, "*")
        if any(name in self.disconnect for name in names):
            raise httpx2.ConnectError("Connection refused", request=request)
        if key == "POST /chat/completions":
            prompt = json.loads(request.content)["messages"][-1]["content"]
            if self.drop_prompts.get(prompt, 0) > 0:
                self.drop_prompts[prompt] -= 1
                raise httpx2.ConnectError("Connection reset by peer", request=request)
        failing = next((name for name in names if name in self.fail), None)
        if failing is not None and self.fail_times.get(failing, 1) > 0:
            if failing in self.fail_times:
                self.fail_times[failing] -= 1
            status, code = self.fail[failing]
            return httpx2.Response(status, json=error_json(code), headers={"retry-after-ms": "1"})
        found = re.search(r"/((?:file|batch)-\d+)", request.url.path)
        item = found.group(1) if found else None

        if key == "POST /chat/completions":
            body = json.loads(request.content)
            prompt = body["messages"][-1]["content"]
            self.chat_bodies.append(body)
            self.chat_calls.append((tag, body["model"], prompt))
            return httpx2.Response(200, json=chat_body(self.reply(prompt)))
        if key == "POST /files":
            parts = multipart(request)
            expires_after = None
            if "expires_after[seconds]" in parts:
                expires_after = {
                    "anchor": parts["expires_after[anchor]"].get_payload(decode=True).decode(),
                    "seconds": int(parts["expires_after[seconds]"].get_payload(decode=True)),
                }
            file_id = self.add_file(
                parts["file"].get_payload(decode=True),
                filename=parts["file"].get_filename(),
                purpose=parts["purpose"].get_payload(decode=True).decode(),
                expires_after=expires_after,
            )
            return httpx2.Response(200, json=self._file_json(file_id, "pending"))
        if key == "GET /files/{id}":
            file = self.files[item]
            status = "processed"
            if file["purpose"] == "batch":
                status = self.file_statuses[min(file["retrieves"], len(self.file_statuses) - 1)]
            file["retrieves"] += 1
            return httpx2.Response(200, json=self._file_json(item, status))
        if key == "GET /files/{id}/content":
            return httpx2.Response(200, content=self.files[item]["content"])
        if key == "DELETE /files/{id}":
            self.deleted.append(item)
            return httpx2.Response(200, json={"id": item, "object": "file", "deleted": True})
        if key == "POST /batches":
            body = json.loads(request.content)
            batch_id = f"batch-{len(self.batches) + 1}"
            lines = [json.loads(raw) for raw in self.files[body["input_file_id"]]["content"].splitlines() if raw]
            rejection = tuple(
                {
                    "code": "url_mismatch",
                    "message": f"The url {line.get('url')!r} doesn't match the batch endpoint {self.batch_url!r}.",
                    "line": number,
                }
                for number, line in enumerate(lines, start=1)
                if line.get("url") != self.batch_url or body["endpoint"] != self.batch_url
            )
            self.batches[batch_id] = {**body, "lines": lines, "retrieves": 0, "status": "validating"}
            self.batches[batch_id].update(cancelled=False, output_file_id=None, error_file_id=None, answered=0)
            self.batches[batch_id]["rejection"] = rejection
            return httpx2.Response(200, json=self._batch_json(batch_id))
        if key == "GET /batches":
            limit = int(request.url.params.get("limit", 20))
            newest_first = [self._batch_json(batch_id) for batch_id in reversed(self.batches)]
            page = newest_first[:limit]
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": page,
                    "first_id": page[0]["id"] if page else None,
                    "last_id": page[-1]["id"] if page else None,
                    "has_more": len(newest_first) > limit,
                },
            )
        if key == "GET /batches/{id}":
            batch = self.batches[item]
            statuses = self.batch_statuses
            if batch["cancelled"]:
                status = "cancelled"
            elif batch["rejection"]:
                status = "failed"  # validation turned it down
            else:
                status = statuses[min(batch["retrieves"], len(statuses) - 1)]
            batch["retrieves"] += 1
            batch["status"] = status
            if status in ("completed", "expired", "cancelled") and not batch["answered"]:
                self._finish(batch, partial=status != "completed")
            return httpx2.Response(200, json=self._batch_json(item))
        if key == "POST /batches/{id}/cancel":
            self.batches[item]["cancelled"] = True
            self.batches[item]["status"] = "cancelling"
            return httpx2.Response(200, json=self._batch_json(item))
        return httpx2.Response(404, json=error_json("NotFound", f"no route for {key}"))

    def _finish(self, batch: dict, *, partial: bool) -> None:
        lines = batch["lines"][: len(batch["lines"]) // 2] if partial else batch["lines"]
        outputs, errors = [], []
        for number, line in enumerate(lines):
            self.batch_lines.append(line)
            prompt = line["body"]["messages"][-1]["content"]
            entry = {"id": f"batch_req_{number}", "custom_id": line["custom_id"], "error": None}
            if prompt in self.line_errors:
                status, code = self.line_errors[prompt]
                entry["response"] = {"status_code": status, "request_id": "r", "body": error_json(code, "refused")}
                errors.append(entry)
            else:
                body = chat_body(self.reply(prompt))
                entry["response"] = {"status_code": 200, "request_id": "r", "body": body}
                outputs.append(entry)
        outputs.reverse()
        batch["answered"] = len(lines) or -1
        batch["completed"], batch["failed"] = len(outputs), len(errors)
        if outputs:
            batch["output_file_id"] = self.add_file("\n".join(json.dumps(o) for o in outputs).encode())
        if errors:
            batch["error_file_id"] = self.add_file("\n".join(json.dumps(e) for e in errors).encode())

    def _file_json(self, file_id: str, status: str) -> dict:
        file = self.files[file_id]
        details = self.status_details if status == "error" else None
        return {
            "id": file_id,
            "object": "file",
            "bytes": len(file["content"]),
            "created_at": 1,
            "filename": file["filename"],
            "purpose": file["purpose"],
            "status": status,
            "status_details": details,
        }

    def _batch_json(self, batch_id: str) -> dict:
        batch = self.batches[batch_id]
        total = len(batch["lines"])
        status = batch["status"]
        completed = batch.get("completed", total // 2 if status == "in_progress" else 0)
        failed = batch.get("failed", 0)
        errors = None
        if status == "failed" and (batch["rejection"] or self.batch_errors):
            errors = {"object": "list", "data": list(batch["rejection"] or self.batch_errors)}
        return {
            "id": batch_id,
            "object": "batch",
            "endpoint": batch["endpoint"],
            "input_file_id": batch["input_file_id"],
            "completion_window": batch["completion_window"],
            "status": status,
            "created_at": 1,
            "output_file_id": batch["output_file_id"],
            "error_file_id": batch["error_file_id"],
            "request_counts": {"total": total, "completed": completed, "failed": failed},
            "errors": errors,
        }


def _route(request: httpx2.Request) -> str:
    path = request.url.path.removeprefix("/openai/v1").removeprefix("/openai")  # the v1 or the classic API
    return re.sub(r"/(?:file|batch)-\d+", "/{id}", path)


def _file_id(request: httpx2.Request) -> str:
    return re.search(r"/(file-\d+)", request.url.path).group(1)


@pytest.fixture
def fake_endpoint(monkeypatch) -> FakeAzure:
    """A fake Azure service behind the openai clients AzureChatClient builds itself from an endpoint.

    The keyword arguments AzureChatClient passed to each SDK client are kept in ``fake.built``.
    """
    fake = FakeAzure()
    fake.built = []

    def build_sync(**kwargs):
        fake.built.append(("sync", kwargs))
        return RealOpenAI(**kwargs, http_client=httpx2.Client(transport=fake.transport("sync")))

    def build_async(**kwargs):
        fake.built.append(("async", kwargs))
        client = RealAsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=fake.transport("async")))
        fake.async_clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", build_sync)
    monkeypatch.setattr(openai, "AsyncOpenAI", build_async)
    return fake


@pytest.fixture
def entra(monkeypatch) -> list[str]:
    """Stands in for azure.identity: each token provider hands out entra-token-1, entra-token-2, ...

    Returns the scopes that token providers were made for.
    """
    import azure.identity

    scopes: list[str] = []

    class Credential:
        pass

    def get_bearer_token_provider(credential, scope):
        assert isinstance(credential, Credential)
        scopes.append(scope)
        issued = 0

        def provider() -> str:
            nonlocal issued
            issued += 1
            return f"entra-token-{issued}"

        return provider

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", get_bearer_token_provider)
    return scopes


def mapreduce(client, clock, **options) -> MapReduce:
    options = {"map_batch_size": 3, "reduce_group_size": 3, "show_progress": False, **options}
    return MapReduce(
        client,
        map_prompt="Summarize: {text}",
        reduce_prompt="Combine: {text}",
        batch_poll_interval=1.0,
        sleep=clock.sleep,
        clock=clock,
        **options,
    )


def reviews(count: int) -> pd.DataFrame:
    return pd.DataFrame({"id": list(range(count)), "review": [f"r{i}" for i in range(count)]})


# What the fake model makes of reviews(7) with groups of 3.
SEVEN_MAPPED = [f"S(r{i})" for i in range(7)]
SEVEN_LEVEL_1 = ["C(S(r0)+S(r1)+S(r2))", "C(S(r3)+S(r4)+S(r5))", "C(S(r6))"]
SEVEN_OUTPUT = "C(" + "+".join(SEVEN_LEVEL_1) + ")"


# --- foundry_base_url and deployment_from_endpoint -------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        pytest.param("my-resource", "https://my-resource.openai.azure.com/openai/v1/", id="resource name"),
        pytest.param("  my-resource\n", "https://my-resource.openai.azure.com/openai/v1/", id="name with spaces"),
        pytest.param("https://my-resource.openai.azure.com/", "https://my-resource.openai.azure.com/openai/v1/"),
        pytest.param("https://my-resource.openai.azure.com", "https://my-resource.openai.azure.com/openai/v1/"),
        pytest.param(
            "https://my-resource.openai.azure.com/openai",
            "https://my-resource.openai.azure.com/openai/v1/",
            id="azure_endpoint style /openai",
        ),
        pytest.param(
            "https://my-hub.services.ai.azure.com/api/projects/my-project",
            "https://my-hub.services.ai.azure.com/openai/v1/",
            id="foundry project endpoint",
        ),
        pytest.param(
            "https://my-hub.services.ai.azure.com/api/projects/my-project/",
            "https://my-hub.services.ai.azure.com/openai/v1/",
            id="foundry project endpoint with slash",
        ),
        pytest.param(
            "https://my-resource.cognitiveservices.azure.com/",
            "https://my-resource.cognitiveservices.azure.com/openai/v1/",
            id="cognitiveservices",
        ),
        pytest.param(
            "https://my-resource.openai.azure.com/openai/v1",
            "https://my-resource.openai.azure.com/openai/v1/",
            id="already v1 without slash",
        ),
        pytest.param(
            "https://my-resource.openai.azure.com/openai/v1/",
            "https://my-resource.openai.azure.com/openai/v1/",
            id="already v1",
        ),
        pytest.param(
            "https://gateway.azure-api.net/team-a/openai/v1/",
            "https://gateway.azure-api.net/team-a/openai/v1/",
            id="gateway already v1",
        ),
        pytest.param(
            "https://gateway.azure-api.net/team-a",
            "https://gateway.azure-api.net/team-a/openai/v1/",
            id="APIM gateway path",
        ),
        pytest.param(
            "https://gateway.azure-api.net/team-a/",
            "https://gateway.azure-api.net/team-a/openai/v1/",
            id="APIM gateway path with slash",
        ),
        pytest.param("http://localhost:8080", "http://localhost:8080/openai/v1/", id="local proxy with port"),
        pytest.param(
            "https://my-resource.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21",
            "https://my-resource.openai.azure.com/openai/v1/",
            id="target URI",
        ),
        pytest.param(
            "https://my-resource.cognitiveservices.azure.com/openai/deployments/gpt-4o/chat/completions"
            "?api-version=2025-01-01-preview",
            "https://my-resource.cognitiveservices.azure.com/openai/v1/",
            id="cognitiveservices target URI",
        ),
        pytest.param(
            "https://my-resource.openai.azure.com/openai/v1/chat/completions",
            "https://my-resource.openai.azure.com/openai/v1/",
            id="v1 operation URL",
        ),
    ],
)
def test_foundry_base_url(endpoint, expected):
    assert foundry_base_url(endpoint) == expected


def test_foundry_base_url_host_name_without_scheme():
    """Resource names can't contain dots, so a dotted value is a host someone pasted without https://."""
    assert foundry_base_url("my-resource.openai.azure.com") == "https://my-resource.openai.azure.com/openai/v1/"


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (
            "https://my-resource.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21",
            "gpt-4o",
        ),
        ("https://my-resource.openai.azure.com/openai/deployments/gpt-4o", "gpt-4o"),
        ("https://my-resource.openai.azure.com/openai/deployments/gpt-4o?api-version=x", "gpt-4o"),
        ("https://my-resource.openai.azure.com/openai/deployments/gpt-4o#frag", "gpt-4o"),
        ("https://my-resource.openai.azure.com/openai/deployments/my%20deployment/chat/completions", "my deployment"),
        ("https://gateway.azure-api.net/team-a/openai/deployments/mini-batch/chat/completions", "mini-batch"),
        ("https://my-resource.openai.azure.com/", None),
        ("https://my-hub.services.ai.azure.com/api/projects/my-project", None),
        ("my-resource", None),
    ],
)
def test_deployment_from_endpoint(endpoint, expected):
    assert deployment_from_endpoint(endpoint) == expected


# --- constructor and from_env ----------------------------------------------------------------------------


def test_needs_an_endpoint_or_a_client():
    with pytest.raises(ConfigError, match="endpoint"):
        AzureChatClient(deployment="gpt-test", api_key="k")
    with pytest.raises(ConfigError, match="endpoint"):
        AzureChatClient("", deployment="gpt-test", api_key="k")


def test_needs_a_deployment():
    with pytest.raises(ConfigError, match="deployment"):
        AzureChatClient("my-resource", api_key="k")
    with pytest.raises(ConfigError, match="deployment"):
        AzureChatClient(client=sdk_client(responding(200)))


@pytest.mark.parametrize("reserved", ["model", "messages", "stream"])
def test_completion_options_cant_set_what_the_framework_fills_in(reserved):
    with pytest.raises(ConfigError, match=reserved):
        AzureChatClient("my-resource", deployment="gpt-test", api_key="k", completion_options={reserved: "x"})


def test_completion_options_are_copied():
    options = {"temperature": 0}
    client = client_on(responding(200), completion_options=options)
    options["temperature"] = 1
    assert client.request_body(MESSAGES)["temperature"] == 0


def test_deployment_comes_from_a_pasted_target_uri(fake_endpoint):
    uri = "https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
    client = AzureChatClient(uri, api_key="k")
    assert client.deployment == "gpt-4o"
    assert client.complete(MESSAGES) == "<Hello>"
    (request,) = fake_endpoint.sent("POST", "/chat/completions")
    assert str(request.url) == BASE_URL + "chat/completions"
    assert json.loads(request.content)["model"] == "gpt-4o"


def test_explicit_deployment_wins_over_the_target_uri(fake_endpoint):
    uri = "https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
    assert AzureChatClient(uri, deployment="gpt-4.1", api_key="k").deployment == "gpt-4.1"


def test_what_each_strategy_can_use():
    both = client_on(responding(200))
    assert (both.supports_batch, both.supports_async, both.supports_sync) == (True, True, True)

    batch_only = client_on(responding(200), deployment=None)
    assert (batch_only.supports_batch, batch_only.supports_async, batch_only.supports_sync) == (True, False, False)

    standard_only = client_on(responding(200), batch_deployment=None)
    assert (standard_only.supports_batch, standard_only.supports_async, standard_only.supports_sync) == (
        False,
        True,
        True,
    )

    no_async = AzureChatClient(client=sdk_client(responding(200)), deployment="gpt-test")
    assert (no_async.supports_async, no_async.supports_sync) == (False, True)


def test_sdk_clients_get_the_base_url_key_retries_and_timeout(fake_endpoint):
    client = AzureChatClient(
        "https://res.openai.azure.com/", deployment="gpt-test", api_key="secret", max_retries=3, timeout=42.0
    )
    asyncio.run(ask_async(client))
    (kind, sync_kwargs), (async_kind, async_kwargs) = fake_endpoint.built
    assert (kind, async_kind) == ("sync", "async")
    for kwargs in (sync_kwargs, async_kwargs):
        # A short connect timeout notices an endpoint that drops packets; the rest of the request gets 42 s.
        assert kwargs == {
            "base_url": BASE_URL,
            "api_key": "secret",
            "max_retries": 3,
            "timeout": httpx2.Timeout(42.0, connect=15.0),
        }


def test_sdk_clients_retry_six_times_by_default(fake_endpoint):
    AzureChatClient("res", deployment="gpt-test", api_key="k")
    assert fake_endpoint.built[0][1]["max_retries"] == 6


def test_from_env_reads_the_settings(fake_endpoint):
    env = {
        "AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-test",
        "AZURE_OPENAI_BATCH_DEPLOYMENT": "gpt-batch",
        "AZURE_OPENAI_API_KEY": "secret",
    }
    client = AzureChatClient.from_env(env)
    assert (client.deployment, client.batch_deployment) == ("gpt-test", "gpt-batch")
    client.complete(MESSAGES)
    (request,) = fake_endpoint.sent("POST", "/chat/completions")
    assert str(request.url) == BASE_URL + "chat/completions"
    assert request.headers["authorization"] == "Bearer secret"


def test_from_env_keyword_arguments_win(fake_endpoint):
    env = {"AZURE_OPENAI_ENDPOINT": "other-resource", "AZURE_OPENAI_DEPLOYMENT": "gpt-test"}
    client = AzureChatClient.from_env(env, endpoint="res", deployment="gpt-override", api_key="k")
    client.complete(MESSAGES)
    (request,) = fake_endpoint.sent("POST", "/chat/completions")
    assert str(request.url) == BASE_URL + "chat/completions"
    assert json.loads(request.content)["model"] == "gpt-override"


def test_from_env_treats_blank_values_as_unset(fake_endpoint):
    env = {
        "AZURE_OPENAI_ENDPOINT": " res ",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-test",
        "AZURE_OPENAI_BATCH_DEPLOYMENT": "   ",
        "AZURE_OPENAI_API_KEY": "k",
    }
    client = AzureChatClient.from_env(env)
    assert client.batch_deployment is None
    assert not client.supports_batch


def test_from_env_without_an_endpoint():
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT"):
        AzureChatClient.from_env({"AZURE_OPENAI_DEPLOYMENT": "gpt-test", "AZURE_OPENAI_API_KEY": "k"})
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT"):
        AzureChatClient.from_env({"AZURE_OPENAI_ENDPOINT": "  ", "AZURE_OPENAI_DEPLOYMENT": "gpt-test"})


def test_from_env_with_a_ready_made_client_needs_no_endpoint():
    client = AzureChatClient.from_env({"AZURE_OPENAI_DEPLOYMENT": "gpt-test"}, client=sdk_client(responding(200)))
    assert client.complete(MESSAGES) == "ok"


def test_from_env_defaults_to_the_process_environment(monkeypatch, fake_endpoint):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "res")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-env")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key")
    monkeypatch.delenv("AZURE_OPENAI_BATCH_DEPLOYMENT", raising=False)
    client = AzureChatClient.from_env()
    assert client.deployment == "gpt-env" and client.batch_deployment is None
    client.complete(MESSAGES)
    (request,) = fake_endpoint.sent("POST", "/chat/completions")
    assert request.headers["authorization"] == "Bearer env-key"


# --- request bodies --------------------------------------------------------------------------------------


def test_request_body_uses_the_right_deployment_and_the_completion_options():
    options = {"temperature": 0, "max_completion_tokens": 800, "reasoning_effort": "low"}
    client = client_on(responding(200), completion_options=options)
    assert client.request_body(MESSAGES) == {"model": "gpt-test", "messages": MESSAGES, **options}
    assert client.request_body(MESSAGES, batch=True) == {"model": "gpt-batch", "messages": MESSAGES, **options}


def test_batch_line_is_one_json_request_for_the_batch_deployment():
    client = client_on(responding(200), completion_options={"temperature": 0})
    messages = [{"role": "user", "content": "Café — naïve\nline two"}]
    raw = client.batch_line("request-7", messages)
    assert "\n" not in raw
    assert raw.isascii()  # non-ASCII text is \u-escaped ...
    assert r"Caf\u00e9 \u2014 na\u00efve\nline two" in raw
    assert json.loads(raw) == {  # ... and reads back as the same text
        "custom_id": "request-7",
        "method": "POST",
        "url": BATCH_ENDPOINT,
        "body": {"model": "gpt-batch", "messages": messages, "temperature": 0},
    }
    assert BATCH_ENDPOINT == "/v1/chat/completions"  # the v1 API's path, as Azure's v1 Batch API expects


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a\u2028b\u2029c", id="line and paragraph separators"),
        pytest.param("a\x85b\x0bc\x0cd\x1ce\r\nf", id="other characters str.splitlines splits on"),
        pytest.param("lone \ud800 surrogate", id="lone surrogate"),
        pytest.param("emoji 🎉 and 中文", id="astral and CJK"),
    ],
)
def test_batch_line_is_ascii_so_no_character_can_split_or_break_the_jsonl_file(text):
    client = client_on(responding(200))
    raw = client.batch_line("request-0", [{"role": "user", "content": text}])
    assert raw.isascii()
    assert raw.splitlines() == [raw]  # even str.splitlines, which splits on \u2028 and the like, sees one line
    assert (raw + "\n").encode("utf-8").count(b"\n") == 1
    assert json.loads(raw)["body"]["messages"][0]["content"] == text


def test_batch_line_byte_size_is_the_size_of_the_escaped_text():
    client = client_on(responding(200))
    short = client.batch_line("request-0", [{"role": "user", "content": "e" * 100}])
    accented = client.batch_line("request-0", [{"role": "user", "content": "é" * 100}])
    assert len(accented.encode("utf-8")) == len(accented)  # one byte per character: it's all ASCII
    assert len(accented) - len(short) == 100 * (len(r"\u00e9") - 1)  # 6 bytes for each é, not 2 as in UTF-8
    astral = client.batch_line("request-0", [{"role": "user", "content": "🎉"}])
    assert r"\ud83c\udf89" in astral  # a surrogate pair: 12 bytes, not 4


def test_complete_sends_the_completion_options():
    seen: list[httpx2.Request] = []
    options = {"temperature": 0, "max_completion_tokens": 50, "response_format": {"type": "json_object"}}
    client = client_on(responding(200, seen=seen), completion_options=options)
    client.complete(MESSAGES)
    assert json.loads(seen[0].content) == {"model": "gpt-test", "messages": MESSAGES, **options}


def test_complete_sends_completion_options_the_sdk_doesnt_know():
    """The docs say completion_options go into every request body; Azure has options the SDK has no argument for."""
    seen: list[httpx2.Request] = []
    options = {"user_security_context": {"application_name": "reviews"}}
    client = client_on(responding(200, seen=seen), completion_options=options)
    assert json.loads(client.batch_line("request-0", MESSAGES))["body"]["user_security_context"] == {
        "application_name": "reviews"
    }
    client.complete(MESSAGES)
    assert json.loads(seen[0].content)["user_security_context"] == {"application_name": "reviews"}
    assert asyncio.run(ask_async(client)) == "ok"
    assert json.loads(seen[1].content)["user_security_context"] == {"application_name": "reviews"}


@pytest.mark.parametrize("route", ["sync", "async"])
def test_completion_options_the_sdk_doesnt_name_land_at_the_top_of_the_json_body(route):
    """Unknown settings travel in the SDK's extra_body, which merges them into the body (no extra_body key)."""
    seen: list[httpx2.Request] = []
    options = {
        "temperature": 0,  # named by the SDK
        "max_completion_tokens": 50,
        "user_security_context": {"application_name": "reviews", "end_user_id": "u-1"},  # not named by the SDK
        "data_sources": [{"type": "azure_search", "parameters": {"index_name": "reviews"}}],
        "some_future_option": None,
    }
    client = client_on(responding(200, seen=seen), completion_options=options)
    if route == "sync":
        assert client.complete(MESSAGES) == "ok"
    else:
        assert asyncio.run(ask_async(client)) == "ok"
    (request,) = seen
    body = json.loads(request.content)
    assert body == {"model": "gpt-test", "messages": MESSAGES, **options}
    assert "extra_body" not in body
    # The same settings in the same place as in a batch input line.
    batch_body = json.loads(client.batch_line("request-0", MESSAGES))["body"]
    assert batch_body == {**body, "model": "gpt-batch"}


def test_completion_options_the_sdk_doesnt_name_reach_an_azure_openai_client_too():
    seen: list[httpx2.Request] = []
    azure_client = openai.AzureOpenAI(
        azure_endpoint="https://res.openai.azure.com",
        api_key="secret",
        api_version="2024-10-21",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(responding(200, seen=seen))),
    )
    options = {"temperature": 0, "user_security_context": {"application_name": "reviews"}}
    client = AzureChatClient(client=azure_client, deployment="gpt-test", completion_options=options)
    assert client.complete(MESSAGES) == "ok"
    assert json.loads(seen[0].content) == {"model": "gpt-test", "messages": MESSAGES, **options}


def test_completion_options_go_by_name_to_a_create_that_takes_any_keyword():
    """A wrapped SDK whose create() takes **kwargs can't be inspected, so every setting goes by name."""
    received: list[dict] = []

    class Completions:
        def create(self, **kwargs):
            received.append(kwargs)
            return openai.types.chat.ChatCompletion.model_validate(chat_body("wrapped"))

    class Chat:
        completions = Completions()

    class Wrapped:
        chat = Chat()

    options = {"temperature": 0, "user_security_context": {"application_name": "reviews"}}
    client = AzureChatClient(client=Wrapped(), deployment="gpt-test", completion_options=options)
    assert client.complete(MESSAGES) == "wrapped"
    assert received == [{"model": "gpt-test", "messages": MESSAGES, **options}]


def test_completion_options_extra_body_is_not_silently_dropped():
    """An SDK user may put Azure-only fields in completion_options["extra_body"]; none of them may vanish.

    They're refused up front (they couldn't go in a batch body either), before any request is sent."""
    seen: list[httpx2.Request] = []
    options = {
        "extra_body": {"data_sources": [{"type": "azure_search"}]},
        "user_security_context": {"application_name": "reviews"},
    }
    with pytest.raises(ConfigError, match="extra_body"):
        client_on(responding(200, seen=seen), completion_options=options)
    assert seen == []
    # The same fields straight in completion_options reach the body.
    client = client_on(responding(200, seen=seen), completion_options={"data_sources": [{"type": "azure_search"}]})
    client.complete(MESSAGES)
    assert json.loads(seen[0].content)["data_sources"] == [{"type": "azure_search"}]


@pytest.mark.parametrize("option", ["extra_body", "extra_headers", "extra_query", "timeout"])
def test_completion_options_cant_set_what_only_shapes_the_sdk_call(option):
    """These go to the SDK, not into the request body, so a batch input line couldn't carry them."""
    with pytest.raises(ConfigError, match=rf"can't set '{option}'.*request body") as caught:
        client_on(responding(200), completion_options={"temperature": 0, option: {"x": 1}})
    assert "batch file" in str(caught.value)
    with pytest.raises(ConfigError, match=option):  # the same for a client built from an endpoint
        AzureChatClient("my-resource", deployment="gpt-test", api_key="k", completion_options={option: 1})


def test_completion_options_with_several_sdk_only_options_name_one():
    with pytest.raises(ConfigError, match="'extra_body'"):
        client_on(responding(200), completion_options={"timeout": 5, "extra_body": {}, "extra_query": {}})


def test_completion_options_that_only_look_like_sdk_options_are_body_fields():
    options = {"extra": 1, "timeout_ms": 5, "extra_bodies": [], "Timeout": 2}
    client = client_on(responding(200), completion_options=options)
    assert client.request_body(MESSAGES) == {"model": "gpt-test", "messages": MESSAGES, **options}


# --- complete() ------------------------------------------------------------------------------------------


def test_complete_posts_to_the_v1_chat_completions_path_with_the_key(fake_endpoint):
    client = AzureChatClient("https://res.openai.azure.com/", deployment="gpt-test", api_key="secret")
    assert client.complete(MESSAGES) == "<Hello>"
    (request,) = fake_endpoint.sent("POST", "/chat/completions")
    assert str(request.url) == "https://res.openai.azure.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    assert "api-version" not in request.url.params  # the v1 API takes none
    assert json.loads(request.content) == {"model": "gpt-test", "messages": MESSAGES}


def test_complete_from_a_foundry_project_endpoint(fake_endpoint):
    client = AzureChatClient("https://res.services.ai.azure.com/api/projects/p1", deployment="gpt-test", api_key="k")
    client.complete(MESSAGES)
    (request,) = fake_endpoint.sent("POST", "/chat/completions")
    assert str(request.url) == "https://res.services.ai.azure.com/openai/v1/chat/completions"


def test_complete_returns_the_stripped_reply():
    client = client_on(responding(200, chat_body("  a summary \n")))
    assert client.complete(MESSAGES) == "a summary"


def test_complete_with_an_azure_openai_client_sends_the_api_key_header():
    seen: list[httpx2.Request] = []
    azure_client = openai.AzureOpenAI(
        azure_endpoint="https://res.openai.azure.com",
        api_key="secret",
        api_version="2024-10-21",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(responding(200, seen=seen))),
    )
    client = AzureChatClient(client=azure_client, deployment="gpt-test")
    assert client.complete(MESSAGES) == "ok"
    (request,) = seen
    assert request.headers["api-key"] == "secret"
    assert request.url.path == "/openai/deployments/gpt-test/chat/completions"
    assert request.url.params["api-version"] == "2024-10-21"


def test_complete_without_a_standard_deployment_is_a_setup_error():
    seen: list[httpx2.Request] = []
    client = client_on(responding(200, seen=seen), deployment=None)
    with pytest.raises(LLMSetupError, match="Batch API"):
        client.complete(MESSAGES)
    assert seen == []


def test_complete_reports_a_content_filtered_reply():
    client = client_on(responding(200, chat_body("partial", finish_reason="content_filter")))
    with pytest.raises(LLMRequestError) as caught:
        client.complete(MESSAGES)
    assert (caught.value.retryable, caught.value.code) == (False, "content_filter")


# --- translate_error: SDK exceptions to "this request failed" or "every request will fail" -----------------


@pytest.mark.parametrize(
    ("status", "code", "retryable", "expected_code", "words"),
    [
        pytest.param(400, "content_filter", False, "content_filter", "content filter", id="400 content_filter"),
        pytest.param(
            400, "ResponsibleAIPolicyViolation", False, "ResponsibleAIPolicyViolation", "content filter", id="400 RAI"
        ),
        pytest.param(
            400, "context_length_exceeded", False, "context_length_exceeded", "too long", id="400 context length"
        ),
        pytest.param(400, "invalid_prompt", False, "invalid_prompt", "rejected", id="400 other"),
        pytest.param(400, None, False, None, "rejected", id="400 without code"),
        pytest.param(422, "unprocessable", False, "unprocessable", "rejected", id="422"),
        pytest.param(429, "429", True, "rate_limit", "throttl", id="429"),
        pytest.param(500, None, True, None, "failed", id="500"),
        pytest.param(503, "ServiceUnavailable", True, "ServiceUnavailable", "failed", id="503"),
        pytest.param(408, None, True, None, "failed", id="408"),
    ],
)
def test_request_errors(status, code, retryable, expected_code, words):
    seen: list[httpx2.Request] = []
    client = client_on(responding(status, error_json(code), seen=seen))
    with pytest.raises(LLMRequestError, match=words) as caught:
        client.complete(MESSAGES)
    assert (caught.value.retryable, caught.value.code) == (retryable, expected_code)
    assert isinstance(caught.value.__cause__, openai.APIStatusError)
    assert caught.value.__cause__.status_code == status

    with pytest.raises(LLMRequestError) as caught_async:  # the async route maps errors the same way
        asyncio.run(ask_async(client))
    assert (caught_async.value.retryable, caught_async.value.code) == (retryable, expected_code)


@pytest.mark.parametrize(
    ("status", "code", "words"),
    [
        pytest.param(401, "invalid_api_key", "credentials", id="401"),
        pytest.param(403, "PermissionDenied", "Cognitive Services OpenAI User", id="403"),
        pytest.param(404, "DeploymentNotFound", 'No deployment named "gpt-test"', id="404"),
        pytest.param(400, "unsupported_parameter", "request settings", id="400 unsupported_parameter"),
        pytest.param(400, "unsupported_value", "request settings", id="400 unsupported_value"),
        pytest.param(400, "OperationNotSupported", "request settings", id="400 OperationNotSupported"),
    ],
)
def test_setup_errors(status, code, words):
    client = client_on(responding(status, error_json(code)))
    with pytest.raises(LLMSetupError, match=words) as caught:
        client.complete(MESSAGES)
    assert isinstance(caught.value.__cause__, openai.APIStatusError)
    with pytest.raises(LLMSetupError, match=words):
        asyncio.run(ask_async(client))


def test_401_message_names_the_status():
    client = client_on(responding(401, error_json("invalid_api_key", "Access denied due to invalid key")))
    with pytest.raises(LLMSetupError, match=r"^Azure rejected the credentials \(401: ") as caught:
        client.complete(MESSAGES)
    message = str(caught.value)
    assert "Access denied due to invalid key" in message  # Azure's own words, which say what's wrong
    assert "Cognitive Services OpenAI User role" in message
    assert "Contributor" not in message  # chat completions only need the User role


@pytest.mark.parametrize(
    ("status", "sdk_error"), [(401, openai.AuthenticationError), (403, openai.PermissionDeniedError)]
)
def test_credential_rejections_on_chat_calls_name_the_user_role(status, sdk_error):
    client = client_on(responding(status, error_json("PermissionDenied", "principal lacks the data action")))
    for call in (lambda: client.complete(MESSAGES), lambda: asyncio.run(ask_async(client))):
        with pytest.raises(LLMSetupError, match=rf"^Azure rejected the credentials \({status}: ") as caught:
            call()
        assert "principal lacks the data action" in str(caught.value)
        assert "Cognitive Services OpenAI User role" in str(caught.value)
        assert type(caught.value.__cause__) is sdk_error


def test_timeout_is_a_retryable_request_error():
    client = client_on(raising(httpx2.ReadTimeout))
    with pytest.raises(LLMRequestError) as caught:
        client.complete(MESSAGES)
    assert (caught.value.retryable, caught.value.code) == (True, "timeout")
    assert isinstance(caught.value.__cause__, openai.APITimeoutError)
    with pytest.raises(LLMRequestError) as caught_async:
        asyncio.run(ask_async(client))
    assert (caught_async.value.retryable, caught_async.value.code) == (True, "timeout")


def test_connection_error_is_a_retryable_request_error():
    """One connection error is just a failed request; the runners stop only on a run of them."""
    client = client_on(raising(httpx2.ConnectError))
    with pytest.raises(LLMRequestError, match="Couldn't connect to Azure OpenAI.*endpoint") as caught:
        client.complete(MESSAGES)
    assert (caught.value.retryable, caught.value.code) == (True, "connection")
    assert type(caught.value.__cause__) is openai.APIConnectionError
    with pytest.raises(LLMRequestError, match="endpoint") as caught_async:
        asyncio.run(ask_async(client))
    assert (caught_async.value.retryable, caught_async.value.code) == (True, "connection")
    assert type(caught_async.value.__cause__) is openai.APIConnectionError


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(error_json("token_limit_exceeded", "Enqueued token limit reached"), id="code"),
        pytest.param(error_json(None, "token_limit_exceeded: enqueued tokens over the limit"), id="message"),
    ],
)
def test_token_limit_exceeded_is_a_retryable_quota_error(body):
    client = client_on(responding(400, body))
    with pytest.raises(LLMRequestError, match="enqueued-token quota is full") as caught:
        client.complete(MESSAGES)
    assert (caught.value.retryable, caught.value.code) == (True, "token_limit_exceeded")
    assert type(caught.value.__cause__) is openai.BadRequestError


def test_translate_error_leaves_unknown_exceptions_to_the_caller():
    for error in (ValueError("odd"), KeyError("x"), RuntimeError("boom"), TypeError("bad argument")):
        assert translate_error(error, "gpt-test") is None
        assert translate_error(error, "gpt-batch", batch=True) is None


def test_translate_error_sign_in_exceptions():
    # DefaultAzureCredential found no credential that works: a setup problem.
    none_worked = translate_error(no_credential_worked(), "gpt-test")
    assert isinstance(none_worked, LLMSetupError)
    assert str(none_worked).startswith("No Entra ID credential worked (DefaultAzureCredential failed to retrieve")
    assert "Environment variables are not fully configured" in str(none_worked)
    assert "az login" in str(none_worked) and "API key" in str(none_worked)
    assert "AZURE_TENANT_ID" in str(none_worked)

    # Once a credential has worked, DefaultAzureCredential calls it directly; its failures can be passing ones.
    unavailable = translate_error(CredentialUnavailableError("the managed identity endpoint didn't answer"), "x")
    assert isinstance(unavailable, LLMRequestError)
    assert (unavailable.retryable, unavailable.code) == (True, "credential")
    assert str(unavailable) == "Couldn't get an Entra ID token: the managed identity endpoint didn't answer"

    rejected = translate_error(ClientAuthenticationError("IMDS endpoint timed out"), "gpt-test")
    assert isinstance(rejected, LLMRequestError)
    assert (rejected.retryable, rejected.code) == (True, "credential")
    assert "IMDS endpoint timed out" in str(rejected)
    # The same with batch=True: sign-in doesn't depend on the route.
    assert isinstance(translate_error(no_credential_worked(), None, batch=True), LLMSetupError)
    assert translate_error(CredentialUnavailableError("x"), None, batch=True).code == "credential"
    assert translate_error(ClientAuthenticationError("x"), None, batch=True).code == "credential"
    # DefaultAzureCredential's words decide, whichever ClientAuthenticationError subclass carries them.
    worded = CredentialUnavailableError("DefaultAzureCredential failed to retrieve a token from the included ...")
    assert isinstance(translate_error(worded, "gpt-test"), LLMSetupError)
    # Other wording from DefaultAzureCredential (e.g. its successful credential failing later) isn't a setup error.
    assert translate_error(ClientAuthenticationError("DefaultAzureCredential: token expired"), "x").retryable


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(credential_unavailable, id="CredentialUnavailableError"),
        pytest.param(client_authentication_failed, id="other ClientAuthenticationError"),
        pytest.param(service_request_error, id="ServiceRequestError"),
        pytest.param(service_response_error, id="ServiceResponseError"),
    ],
)
@pytest.mark.parametrize("batch", [False, True])
def test_translate_error_passing_sign_in_failures_are_retryable_credential_errors(make_error, batch):
    error = make_error()
    translated = translate_error(error, "gpt-test", batch=batch)
    assert type(translated) is LLMRequestError
    assert (translated.retryable, translated.code) == (True, "credential")
    assert str(translated) == f"Couldn't get an Entra ID token: {error}"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: AzureError("x"), id="AzureError"),
        pytest.param(lambda: HttpResponseError("x"), id="HttpResponseError"),
        pytest.param(lambda: ResourceNotFoundError("x"), id="ResourceNotFoundError"),
        pytest.param(lambda: RuntimeError("DefaultAzureCredential failed to retrieve a token"), id="phrase elsewhere"),
    ],
)
def test_translate_error_leaves_other_azure_exceptions_to_the_caller(error):
    """Only sign-in failures are translated; other exceptions are someone's bug, raised as they are."""
    assert translate_error(error(), "gpt-test") is None
    assert translate_error(error(), None, batch=True) is None


def test_unknown_exceptions_from_the_sdk_propagate_as_they_are():
    """Callers re-raise what translate_error doesn't know, unwrapped, so bugs aren't disguised as Azure errors."""
    error = RuntimeError("a bug in a custom transport")

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    client = client_on(handler)
    for call in (
        lambda: client.complete(MESSAGES),
        lambda: asyncio.run(ask_async(client)),
        lambda: client.upload_batch_file(b"{}\n"),
        lambda: client.get_batch("batch-1"),
    ):
        with pytest.raises(RuntimeError) as caught:
            call()
        assert caught.value is error


def test_other_sdk_errors_are_retryable():
    error = translate_error(openai.OpenAIError("something odd"), "gpt-test")
    assert isinstance(error, LLMRequestError)
    assert error.retryable
    assert "something odd" in str(error)


def test_only_one_http_request_per_call_with_retries_off():
    seen: list[httpx2.Request] = []
    client = client_on(responding(429, error_json("429"), seen=seen))
    with pytest.raises(LLMRequestError):
        client.complete(MESSAGES)
    assert len(seen) == 1


# --- completion_text -------------------------------------------------------------------------------------


def test_completion_text_returns_the_stripped_reply():
    assert completion_text(chat_body("  hello \n")) == "hello"


def test_completion_text_content_filter():
    for content in ("partial reply", None):
        with pytest.raises(LLMRequestError, match="content filter") as caught:
            completion_text(chat_body(content, finish_reason="content_filter"))
        assert (caught.value.retryable, caught.value.code) == (False, "content_filter")


def test_completion_text_length_with_text_is_kept_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="azure_mapreduce.client"):
        assert completion_text(chat_body("cut sho", finish_reason="length")) == "cut sho"
    assert "max_completion_tokens" in caplog.text


@pytest.mark.parametrize("content", [None, "", "   "])
def test_completion_text_length_without_text(content):
    """A reasoning model can spend every output token thinking; sending it again the same way won't help."""
    with pytest.raises(LLMRequestError, match="max_completion_tokens") as caught:
        completion_text(chat_body(content, finish_reason="length"))
    assert (caught.value.retryable, caught.value.code) == (False, "length")


def test_completion_text_refusal():
    with pytest.raises(LLMRequestError, match="I can't help with that") as caught:
        completion_text(chat_body(None, refusal="I can't help with that"))
    assert (caught.value.retryable, caught.value.code) == (False, "refusal")


def test_completion_text_refusal_with_text_keeps_the_text():
    assert completion_text(chat_body("here you go", refusal="partly")) == "here you go"


@pytest.mark.parametrize("content", [None, "", " \n\t "])
def test_completion_text_empty_reply_is_retryable(content):
    with pytest.raises(LLMRequestError, match="empty") as caught:
        completion_text(chat_body(content))
    assert (caught.value.retryable, caught.value.code) == (True, "empty")


@pytest.mark.parametrize("body", [{"choices": []}, {}, {"choices": None}])
def test_completion_text_no_choices_is_retryable(body):
    with pytest.raises(LLMRequestError, match="no choices") as caught:
        completion_text(body)
    assert caught.value.retryable


def test_completion_text_message_missing():
    with pytest.raises(LLMRequestError, match="empty"):
        completion_text({"choices": [{"index": 0, "finish_reason": "stop"}]})


# --- async_session ---------------------------------------------------------------------------------------


def test_async_session_answers_and_closes_its_client():
    fake = FakeAzure()
    client = fake.client(completion_options={"temperature": 0})

    async def main():
        async with client.async_session() as complete:
            replies = await asyncio.gather(*(complete([{"role": "user", "content": f"q{i}"}]) for i in range(3)))
            assert not fake.async_clients[0].is_closed()
        return replies

    assert asyncio.run(main()) == ["<q0>", "<q1>", "<q2>"]
    assert len(fake.async_clients) == 1
    assert fake.async_clients[0].is_closed()
    assert {tag for tag, _, _ in fake.chat_calls} == {"async"}
    assert all(body["model"] == "gpt-test" and body["temperature"] == 0 for body in fake.chat_bodies)


def test_async_session_closes_its_client_when_the_block_fails():
    fake = FakeAzure()
    client = fake.client()

    async def main():
        async with client.async_session() as complete:
            await complete(MESSAGES)
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(main())
    assert fake.async_clients[0].is_closed()


def test_each_async_session_gets_a_fresh_client():
    fake = FakeAzure()
    client = fake.client()
    assert asyncio.run(ask_async(client)) == "<Hello>"
    assert asyncio.run(ask_async(client)) == "<Hello>"  # a second event loop: the first client can't be reused
    assert len(fake.async_clients) == 2
    assert all(async_client.is_closed() for async_client in fake.async_clients)


def test_async_session_without_an_async_client_is_a_setup_error():
    client = AzureChatClient(client=sdk_client(responding(200)), deployment="gpt-test")
    with pytest.raises(LLMSetupError, match="async_client_factory"):
        asyncio.run(ask_async(client))


def test_async_session_built_from_the_endpoint(fake_endpoint):
    client = AzureChatClient("https://res.openai.azure.com/", deployment="gpt-test", api_key="secret")
    assert asyncio.run(ask_async(client)) == "<Hello>"
    ((tag, request),) = fake_endpoint.requests
    assert tag == "async"
    assert str(request.url) == BASE_URL + "chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    assert fake_endpoint.async_clients[0].is_closed()


# --- Entra ID --------------------------------------------------------------------------------------------


def test_entra_id_sends_a_fresh_bearer_token_on_each_request(fake_endpoint, entra):
    client = AzureChatClient("res", deployment="gpt-test")
    client.complete(MESSAGES)
    client.complete(MESSAGES)
    assert [r.headers["authorization"] for r in fake_endpoint.sent("POST", "/chat/completions")] == [
        "Bearer entra-token-1",
        "Bearer entra-token-2",
    ]
    assert entra == [FOUNDRY_SCOPE]
    assert FOUNDRY_SCOPE == "https://ai.azure.com/.default"


def test_entra_id_async_calls_share_the_token_provider(fake_endpoint, entra):
    client = AzureChatClient("res", deployment="gpt-test")
    client.complete(MESSAGES)
    assert asyncio.run(ask_async(client)) == "<Hello>"
    sync_request, async_request = fake_endpoint.sent("POST", "/chat/completions")
    assert sync_request.headers["authorization"] == "Bearer entra-token-1"
    assert async_request.headers["authorization"] == "Bearer entra-token-2"
    assert entra == [FOUNDRY_SCOPE]  # one credential and provider for both


def test_entra_id_token_scope_can_be_changed(fake_endpoint, entra):
    AzureChatClient("res", deployment="gpt-test", token_scope="https://cognitiveservices.azure.com/.default")
    assert entra == ["https://cognitiveservices.azure.com/.default"]


def test_api_key_skips_entra_id(fake_endpoint, entra):
    client = AzureChatClient("res", deployment="gpt-test", api_key="k")
    client.complete(MESSAGES)
    asyncio.run(ask_async(client))
    assert entra == []


@pytest.fixture
def failing_sign_in(monkeypatch) -> Callable[[BaseException], list[str]]:
    """Stands in for azure.identity with a token provider that raises the exception it's given.

    Returns the scopes each token request was made for.
    """
    import azure.identity

    def use(error: BaseException) -> list[str]:
        requested: list[str] = []

        class Credential:
            pass

        def get_bearer_token_provider(credential, scope):
            def provider() -> str:
                requested.append(scope)
                raise error

            return provider

        monkeypatch.setattr(azure.identity, "DefaultAzureCredential", Credential)
        monkeypatch.setattr(azure.identity, "get_bearer_token_provider", get_bearer_token_provider)
        return requested

    return use


@pytest.fixture
def no_working_credential(monkeypatch) -> list[dict]:
    """The real azure.identity DefaultAzureCredential and token provider, on a machine with no credential.

    Only EnvironmentCredential stays in the chain (the others would look for az, a managed identity...), and the
    environment variables it reads are unset, so every token request fails the way it does out of the box.
    Returns the keyword arguments of each DefaultAzureCredential made.
    """
    import azure.identity

    for name in list(os.environ):
        if name.startswith(("AZURE_", "IDENTITY_", "MSI_")):
            monkeypatch.delenv(name)
    real = azure.identity.DefaultAzureCredential
    others = ("managed_identity", "cli", "powershell", "developer_cli", "visual_studio_code", "shared_token_cache")
    others += ("interactive_browser", "workload_identity", "broker")
    exclusions = {f"exclude_{name}_credential": True for name in others}
    made: list[dict] = []

    def default_azure_credential(**kwargs):
        made.append(kwargs)
        return real(**exclusions, **kwargs)

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", default_azure_credential)
    return made


ENTRA_ROUTES = {
    "sync": lambda client: client.complete(MESSAGES),
    "async": lambda client: asyncio.run(ask_async(client)),
    "batch upload": lambda client: client.upload_batch_file(b"{}\n"),
    "batch create": lambda client: client.create_batch("file-1"),
    "batch poll": lambda client: client.get_batch("batch-1"),
}


@pytest.mark.parametrize("route", list(ENTRA_ROUTES))
def test_entra_id_without_a_credential_is_a_setup_error(fake_endpoint, failing_sign_in, route):
    error = no_credential_worked()
    requested = failing_sign_in(error)
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch")
    with pytest.raises(LLMSetupError, match=r"^No Entra ID credential worked \(DefaultAzureCredential") as caught:
        ENTRA_ROUTES[route](client)
    assert "Azure CLI not found on path" in str(caught.value)  # what DefaultAzureCredential tried, and why not
    assert "az login" in str(caught.value)
    assert caught.value.__cause__ is error
    assert requested == [FOUNDRY_SCOPE]  # a setup error: nothing else was tried (no looking for the batch job)
    assert fake_endpoint.requests == []  # nothing went out without a token


@pytest.mark.parametrize("route", list(ENTRA_ROUTES))
def test_entra_id_with_the_real_default_azure_credential_and_no_credential(fake_endpoint, no_working_credential, route):
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch")
    with pytest.raises(
        LLMSetupError, match=r"^No Entra ID credential worked \(DefaultAzureCredential failed"
    ) as caught:
        ENTRA_ROUTES[route](client)
    assert type(caught.value.__cause__) is ClientAuthenticationError
    assert "EnvironmentCredential authentication unavailable" in str(caught.value)
    assert "Pass an API key, run `az login`" in str(caught.value)
    assert no_working_credential == [{}]  # the one AzureChatClient made for itself
    assert fake_endpoint.requests == []


@pytest.mark.parametrize(
    "make_error",
    [credential_unavailable, client_authentication_failed, service_request_error, service_response_error],
)
@pytest.mark.parametrize("route", list(ENTRA_ROUTES))
def test_entra_id_token_failure_is_a_retryable_credential_error(fake_endpoint, failing_sign_in, route, make_error):
    """A failed token request can be a passing hiccup (a managed identity endpoint timing out)."""
    error = make_error()
    failing_sign_in(error)
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch")
    with pytest.raises(LLMRequestError, match="^Couldn't get an Entra ID token: ") as caught:
        ENTRA_ROUTES[route](client)
    assert (caught.value.retryable, caught.value.code) == (True, "credential")
    assert str(error) in str(caught.value)
    assert caught.value.__cause__ is error
    assert fake_endpoint.requests == []


@pytest.mark.parametrize("route", list(ENTRA_ROUTES))
def test_entra_id_unknown_sign_in_exception_propagates_as_it_is(fake_endpoint, failing_sign_in, route):
    error = ValueError("a bug in the credential chain")
    failing_sign_in(error)
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch")
    with pytest.raises(ValueError) as caught:
        ENTRA_ROUTES[route](client)
    assert caught.value is error


# --- Batch API primitives --------------------------------------------------------------------------------


def test_upload_batch_file_sends_a_jsonl_file_for_the_batch_purpose():
    fake = FakeAzure()
    client = fake.client()
    payload = "".join(client.batch_line(f"request-{i}", MESSAGES) + "\n" for i in range(2)).encode()
    file_id = client.upload_batch_file(payload)
    (request,) = fake.sent("POST", "/files")
    assert str(request.url) == BASE_URL + "files"
    assert request.headers["content-type"].startswith("multipart/form-data")
    parts = multipart(request)
    assert parts["purpose"].get_payload(decode=True) == b"batch"
    assert parts["file"].get_filename().endswith(".jsonl")
    assert parts["file"].get_payload(decode=True) == payload
    assert file_id == "file-1"
    assert fake.files[file_id]["content"] == payload


def test_file_status_reads_the_processing_status():
    fake = FakeAzure(file_statuses=("pending", "processed"))
    client = fake.client()
    file_id = client.upload_batch_file(b"{}\n")
    assert client.file_status(file_id) == ("pending", None)
    assert client.file_status(file_id) == ("processed", None)
    assert [str(request.url) for request in fake.sent("GET", "/files/{id}")] == [BASE_URL + f"files/{file_id}"] * 2


def test_file_status_carries_the_error_details():
    fake = FakeAzure(file_statuses=("error",), status_details="Line 2: invalid JSON")
    client = fake.client()
    file_id = client.upload_batch_file(b"{}\n")
    assert client.file_status(file_id) == ("error", "Line 2: invalid JSON")


def test_create_batch_posts_the_input_file_endpoint_and_window():
    fake = FakeAzure()
    client = fake.client()
    file_id = client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode())
    job = client.create_batch(file_id)
    (request,) = fake.sent("POST", "/batches")
    assert str(request.url) == BASE_URL + "batches"
    assert json.loads(request.content) == {
        "input_file_id": file_id,
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
        "output_expires_after": {"anchor": "created_at", "seconds": 1209600},
    }
    assert job == BatchJob(id="batch-1", status="validating", total=1)


# --- batch file expiry -----------------------------------------------------------------------------------


def test_upload_batch_file_asks_azure_to_expire_the_file_after_14_days():
    """So input files left behind by an interrupted run don't pile up against the resource's file limit."""
    fake = FakeAzure()
    client = fake.client()
    assert client.batch_file_expiry == BATCH_FILE_EXPIRY_SECONDS == 14 * 24 * 3600 == 1209600
    client.upload_batch_file(b"{}\n")
    (request,) = fake.sent("POST", "/files")
    parts = multipart(request)
    assert set(parts) == {"purpose", "file", "expires_after[anchor]", "expires_after[seconds]"}
    assert parts["expires_after[anchor]"].get_payload(decode=True) == b"created_at"
    assert parts["expires_after[seconds]"].get_payload(decode=True) == b"1209600"
    assert parts["purpose"].get_payload(decode=True) == b"batch"
    assert fake.files["file-1"]["expires_after"] == {"anchor": "created_at", "seconds": 1209600}


def test_create_batch_asks_azure_to_expire_the_output_files_after_14_days():
    fake = FakeAzure()
    client = fake.client()
    client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    (request,) = fake.sent("POST", "/batches")
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content)["output_expires_after"] == {"anchor": "created_at", "seconds": 1209600}


def test_batch_file_expiry_can_be_changed():
    fake = FakeAzure()
    client = fake.client(batch_file_expiry=30 * 24 * 3600)
    client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    (upload,) = fake.sent("POST", "/files")
    assert multipart(upload)["expires_after[seconds]"].get_payload(decode=True) == b"2592000"
    (create,) = fake.sent("POST", "/batches")
    assert json.loads(create.content)["output_expires_after"] == {"anchor": "created_at", "seconds": 2592000}


def test_batch_file_expiry_none_keeps_files_until_deleted():
    fake = FakeAzure()
    client = fake.client(batch_file_expiry=None)
    file_id = client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode())
    client.create_batch(file_id)
    (upload,) = fake.sent("POST", "/files")
    assert set(multipart(upload)) == {"purpose", "file"}
    assert b"expires_after" not in upload.content
    (create,) = fake.sent("POST", "/batches")
    assert json.loads(create.content) == {
        "input_file_id": file_id,
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
    }


@pytest.mark.parametrize("expiry", [BATCH_FILE_EXPIRY_SECONDS, 20 * 24 * 3600, 30 * 24 * 3600, None])
def test_batch_file_expiry_from_14_to_30_days_or_none(expiry):
    assert client_on(responding(200), batch_file_expiry=expiry).batch_file_expiry == expiry
    legacy = AzureChatClient(client=legacy_sdk_client(responding(200)), batch_deployment="b", batch_file_expiry=expiry)
    assert legacy.batch_file_expiry == expiry


@pytest.mark.parametrize(
    "expiry",
    [
        pytest.param(0, id="0"),
        pytest.param(-1, id="negative"),
        pytest.param(3600, id="an hour"),
        pytest.param(BATCH_FILE_EXPIRY_SECONDS - 1, id="a second under 14 days"),
        pytest.param(30 * 24 * 3600 + 1, id="a second over 30 days"),
        pytest.param(True, id="True"),
        pytest.param(False, id="False"),
        pytest.param(1209600.0, id="whole float"),
        pytest.param(1209600.5, id="fractional float"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param("1209600", id="numeric string"),
        pytest.param("AUTO", id="AUTO"),
        pytest.param("", id="empty string"),
        pytest.param([1209600], id="list"),
    ],
)
def test_batch_file_expiry_outside_what_azure_accepts_is_a_config_error(fake_endpoint, expiry):
    words = r"^batch_file_expiry must be None or a whole number of seconds from 1209600 \(14 days\) to 2592000 \(30"
    with pytest.raises(ConfigError, match=words) as caught:
        client_on(responding(200), batch_file_expiry=expiry)
    assert f"(got {expiry!r})" in str(caught.value)
    with pytest.raises(ConfigError, match=words):
        AzureChatClient(client=legacy_sdk_client(responding(200)), batch_deployment="b", batch_file_expiry=expiry)
    with pytest.raises(ConfigError, match=words):
        AzureChatClient("res", deployment="gpt-test", batch_file_expiry=expiry)
    assert fake_endpoint.built == []  # refused before any SDK client (or Entra ID credential) was made


def test_batch_file_expiry_auto_is_the_default():
    assert client_on(responding(200), batch_file_expiry="auto").batch_file_expiry == BATCH_FILE_EXPIRY_SECONDS
    legacy = AzureChatClient(client=legacy_sdk_client(responding(200)), batch_deployment="b", batch_file_expiry="auto")
    assert legacy.batch_file_expiry is None


# --- a legacy openai.AzureOpenAI client ------------------------------------------------------------------


def test_a_legacy_azure_openai_client_gets_the_classic_batch_path_and_no_file_expiry():
    client = AzureChatClient(client=legacy_sdk_client(responding(200)), batch_deployment="gpt-batch")
    assert client.batch_endpoint == LEGACY_BATCH_ENDPOINT == "/chat/completions"
    assert client.batch_file_expiry is None  # the older API versions don't take expires_after
    assert json.loads(client.batch_line("request-0", MESSAGES))["url"] == "/chat/completions"


def test_a_legacy_azure_openai_client_on_the_wire():
    fake = FakeAzure(batch_url="/chat/completions")
    client = AzureChatClient(client=fake.legacy_sdk_client(), batch_deployment="gpt-batch")
    file_id = client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode())
    job = client.create_batch(file_id)

    (upload,) = fake.sent("POST", "/files")
    assert (upload.url.path, upload.url.params["api-version"]) == ("/openai/files", "2024-10-21")
    assert upload.headers["api-key"] == "secret"
    assert set(multipart(upload)) == {"purpose", "file"}
    assert b"expires_after" not in upload.content
    (create,) = fake.sent("POST", "/batches")
    assert (create.url.path, create.url.params["api-version"]) == ("/openai/batches", "2024-10-21")
    assert json.loads(create.content) == {
        "input_file_id": file_id,
        "endpoint": "/chat/completions",
        "completion_window": "24h",
    }
    assert client.get_batch(job.id).status == "in_progress"
    assert client.get_batch(job.id).status == "completed"


def test_explicit_batch_settings_win_over_the_legacy_client_defaults():
    """E.g. a classic client pointed at a gateway that serves the v1 Batch API."""
    fake = FakeAzure()  # the v1 Batch API: only /v1/chat/completions
    client = AzureChatClient(
        client=fake.legacy_sdk_client(),
        batch_deployment="gpt-batch",
        batch_endpoint=BATCH_ENDPOINT,
        batch_file_expiry=30 * 24 * 3600,
    )
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    (upload,) = fake.sent("POST", "/files")
    assert multipart(upload)["expires_after[seconds]"].get_payload(decode=True) == b"2592000"
    (create,) = fake.sent("POST", "/batches")
    assert json.loads(create.content)["endpoint"] == "/v1/chat/completions"
    assert json.loads(create.content)["output_expires_after"] == {"anchor": "created_at", "seconds": 2592000}
    assert client.get_batch(job.id).status == "in_progress"  # not failed: Azure took the lines and the job


def test_a_v1_client_with_an_explicit_classic_path_and_no_expiry():
    client = client_on(responding(200), batch_endpoint=LEGACY_BATCH_ENDPOINT, batch_file_expiry=None)
    assert (client.batch_endpoint, client.batch_file_expiry) == ("/chat/completions", None)


def test_a_client_built_from_the_endpoint_uses_the_v1_batch_settings(fake_endpoint):
    client = AzureChatClient("res", batch_deployment="gpt-batch", api_key="k")
    assert (client.batch_endpoint, client.batch_file_expiry) == (BATCH_ENDPOINT, BATCH_FILE_EXPIRY_SECONDS)


def test_an_azure_openai_client_on_the_v1_base_url_gets_the_v1_batch_settings():
    fake = FakeAzure()
    sdk = RealAzureOpenAI(
        base_url=BASE_URL,
        api_key="secret",
        api_version="preview",
        max_retries=0,
        http_client=httpx2.Client(transport=fake.transport("sync")),
    )
    client = AzureChatClient(client=sdk, batch_deployment="gpt-batch")
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    (create,) = fake.sent("POST", "/batches")
    assert create.url.path == "/openai/v1/batches"  # the v1 API ...
    assert client.get_batch(job.id).status == "in_progress"  # ... so the v1 path, or the job fails validation
    assert (client.batch_endpoint, client.batch_file_expiry) == (BATCH_ENDPOINT, BATCH_FILE_EXPIRY_SECONDS)


# --- batch_endpoint --------------------------------------------------------------------------------------


def test_batch_endpoint_defaults_to_the_v1_path():
    client = client_on(responding(200))
    assert client.batch_endpoint == BATCH_ENDPOINT == "/v1/chat/completions"
    assert json.loads(client.batch_line("request-0", MESSAGES))["url"] == "/v1/chat/completions"


def test_batch_endpoint_override_goes_into_the_lines_and_the_job():
    """E.g. a gateway whose Batch API takes the path without /v1."""
    fake = FakeAzure(batch_url="/chat/completions")
    client = fake.client(batch_endpoint="/chat/completions")
    assert client.batch_endpoint == "/chat/completions"
    lines = "".join(client.batch_line(f"request-{i}", MESSAGES) + "\n" for i in range(2))
    assert [json.loads(raw)["url"] for raw in lines.splitlines()] == ["/chat/completions"] * 2
    job = client.create_batch(client.upload_batch_file(lines.encode()))
    (request,) = fake.sent("POST", "/batches")
    assert json.loads(request.content)["endpoint"] == "/chat/completions"
    assert client.get_batch(job.id).status == "in_progress"
    assert client.get_batch(job.id).status == "completed"


def test_the_v1_batch_api_rejects_lines_for_another_url():
    """The fake mirrors Azure: a line whose url isn't the job's /v1/chat/completions fails validation."""
    fake = FakeAzure()
    client = fake.client(batch_endpoint="/chat/completions")
    lines = "".join(client.batch_line(f"request-{i}", MESSAGES) + "\n" for i in range(2))
    job = client.create_batch(client.upload_batch_file(lines.encode()))
    failed = client.get_batch(job.id)
    assert failed.status == "failed"
    assert failed.error_codes == ("url_mismatch", "url_mismatch")
    assert failed.errors[0].startswith("url_mismatch: ")
    assert failed.errors[0].endswith("(line 1)") and failed.errors[1].endswith("(line 2)")
    assert failed.output_file_id is None and failed.error_file_id is None


def test_the_v1_batch_api_rejects_a_line_whose_url_differs_from_the_rest():
    fake = FakeAzure()
    client = fake.client()
    good = client.batch_line("request-0", MESSAGES)
    bad = json.dumps({**json.loads(client.batch_line("request-1", MESSAGES)), "url": "/v1/embeddings"})
    job = client.create_batch(client.upload_batch_file(f"{good}\n{bad}\n".encode()))
    failed = client.get_batch(job.id)
    assert (failed.status, failed.error_codes) == ("failed", ("url_mismatch",))
    assert failed.errors[0].endswith("(line 2)")


# --- Batch API errors ------------------------------------------------------------------------------------

BATCH_PRIMITIVES: dict[str, Callable[[AzureChatClient], Any]] = {
    "upload_batch_file": lambda client: client.upload_batch_file(b"{}\n"),
    "file_status": lambda client: client.file_status("file-1"),
    "create_batch": lambda client: client.create_batch("file-1"),
    "get_batch": lambda client: client.get_batch("batch-1"),
    "cancel_batch": lambda client: client.cancel_batch("batch-1"),
    "read_file": lambda client: client.read_file("file-1"),
    "delete_file": lambda client: client.delete_file("file-1"),
}


@pytest.mark.parametrize("primitive", list(BATCH_PRIMITIVES))
@pytest.mark.parametrize(
    ("status", "sdk_error"), [(401, openai.AuthenticationError), (403, openai.PermissionDeniedError)]
)
def test_batch_api_credential_rejections_name_the_contributor_role(primitive, status, sdk_error):
    """Uploading files and creating jobs needs more than the OpenAI User role that chat calls need."""
    client = client_on(responding(status, error_json("PermissionDenied", "lacks Microsoft.CognitiveServices/files")))
    with pytest.raises(LLMSetupError, match=rf"^Azure rejected the credentials \({status}: ") as caught:
        BATCH_PRIMITIVES[primitive](client)
    message = str(caught.value)
    assert "Cognitive Services OpenAI Contributor role" in message
    assert "lacks Microsoft.CognitiveServices/files" in message
    assert type(caught.value.__cause__) is sdk_error
    assert caught.value.__cause__.status_code == status


@pytest.mark.parametrize("primitive", list(BATCH_PRIMITIVES))
def test_batch_api_404_is_a_setup_error_about_the_batch_api(primitive):
    client = client_on(responding(404, error_json("404", "Resource not found")))
    with pytest.raises(LLMSetupError, match="Batch API call was answered with 404") as caught:
        BATCH_PRIMITIVES[primitive](client)
    message = str(caught.value)
    assert "Resource not found" in message
    assert "batch deployment" in message
    assert "No deployment named" not in message  # that's the chat call's message
    assert type(caught.value.__cause__) is openai.NotFoundError


@pytest.mark.parametrize("primitive", ["upload_batch_file", "create_batch"])
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(error_json("token_limit_exceeded", "Enqueued token limit reached for gpt-batch"), id="code"),
        pytest.param(error_json(None, "token_limit_exceeded: Enqueued token limit reached"), id="message"),
    ],
)
def test_batch_api_quota_error_is_retryable(primitive, body):
    client = client_on(responding(400, body))
    with pytest.raises(LLMRequestError, match="enqueued-token quota is full") as caught:
        BATCH_PRIMITIVES[primitive](client)
    assert (caught.value.retryable, caught.value.code) == (True, "token_limit_exceeded")
    assert "Enqueued token limit reached" in str(caught.value)
    assert type(caught.value.__cause__) is openai.BadRequestError


@pytest.mark.parametrize("primitive", list(BATCH_PRIMITIVES))
@pytest.mark.parametrize(
    ("status", "code", "expected_code", "sdk_error"),
    [
        pytest.param(500, "server_error", "server_error", openai.InternalServerError, id="500"),
        pytest.param(503, None, None, openai.InternalServerError, id="503"),
        pytest.param(429, "429", "rate_limit", openai.RateLimitError, id="429"),
    ],
)
def test_batch_api_server_errors_are_retryable(primitive, status, code, expected_code, sdk_error):
    client = client_on(responding(status, error_json(code)))
    with pytest.raises(LLMRequestError) as caught:
        BATCH_PRIMITIVES[primitive](client)
    assert (caught.value.retryable, caught.value.code) == (True, expected_code)
    assert type(caught.value.__cause__) is sdk_error


@pytest.mark.parametrize("primitive", list(BATCH_PRIMITIVES))
def test_batch_api_rejected_request_is_a_final_request_error(primitive):
    client = client_on(responding(400, error_json("invalid_request", "input_file_id is not a batch file")))
    with pytest.raises(LLMRequestError, match="input_file_id is not a batch file") as caught:
        BATCH_PRIMITIVES[primitive](client)
    assert (caught.value.retryable, caught.value.code) == (False, "invalid_request")
    assert type(caught.value.__cause__) is openai.BadRequestError


@pytest.mark.parametrize("primitive", list(BATCH_PRIMITIVES))
@pytest.mark.parametrize(
    ("error", "code", "sdk_error"),
    [
        pytest.param(httpx2.ConnectError, "connection", openai.APIConnectionError, id="connection"),
        pytest.param(httpx2.ReadTimeout, "timeout", openai.APITimeoutError, id="timeout"),
    ],
)
def test_batch_api_network_errors_are_retryable(primitive, error, code, sdk_error):
    client = client_on(raising(error))
    with pytest.raises(LLMRequestError) as caught:
        BATCH_PRIMITIVES[primitive](client)
    assert (caught.value.retryable, caught.value.code) == (True, code)
    assert type(caught.value.__cause__) is sdk_error


def test_batch_api_errors_through_the_fake_service():
    """The same translation on a service that fails only some routes: the others keep working."""
    fake = FakeAzure(fail={"GET /batches/{id}": (500, "server_error"), "DELETE /files/{id}": (403, "AuthFailed")})
    client = fake.client()
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    with pytest.raises(LLMRequestError) as polled:
        client.get_batch(job.id)
    assert (polled.value.retryable, polled.value.code) == (True, "server_error")
    with pytest.raises(LLMSetupError, match="Contributor"):
        client.delete_file("file-1")
    assert client.cancel_batch(job.id).status == "cancelling"


# --- create_batch: never sent twice; a job Azure started despite a failure is adopted ----------------------

QUICK_RETRY = {"retry-after-ms": "1"}  # so an SDK retry, where there is one, doesn't slow the tests down


def batch_json(batch_id: str, input_file_id: str, status: str = "in_progress") -> dict:
    return {
        "id": batch_id,
        "object": "batch",
        "endpoint": BATCH_ENDPOINT,
        "input_file_id": input_file_id,
        "completion_window": "24h",
        "status": status,
        "created_at": 1,
        "request_counts": {"total": 4, "completed": 1, "failed": 0},
    }


def batch_service(
    seen: list[httpx2.Request],
    *,
    create: int | type[Exception] | tuple[int, dict] = 200,
    listed: list[dict] = (),
    list_status: int = 200,
):
    """A Batch API whose POST /batches answers ``create`` (a status, a status and a body, or an httpx2 exception
    the transport raises) and whose job list holds ``listed``. Other calls fail with a 500, which the SDK retries."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        key = f"{request.method} {_route(request)}"
        if key == "POST /batches":
            if isinstance(create, type):
                raise create("The read operation timed out", request=request)
            if create == 200:
                input_file_id = json.loads(request.content)["input_file_id"]
                return httpx2.Response(200, json=batch_json("batch-new", input_file_id, "validating"))
            status, body = create if isinstance(create, tuple) else (create, error_json("server_error", "Azure broke"))
            return httpx2.Response(status, json=body, headers=QUICK_RETRY)
        if key == "GET /batches" and list_status == 200:
            return httpx2.Response(200, json={"object": "list", "data": list(listed), "has_more": False})
        return httpx2.Response(
            list_status if key == "GET /batches" else 500, json=error_json(None), headers=QUICK_RETRY
        )

    return handler


def calls(seen: list[httpx2.Request]) -> list[str]:
    return [f"{request.method} {_route(request)}" for request in seen]


RETRYING_SDK_CLIENTS: dict[str, Callable[[Callable], openai.OpenAI]] = {
    "v1": lambda handler: RealOpenAI(
        base_url=BASE_URL,
        api_key="k",
        max_retries=3,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    ),
    "legacy": lambda handler: legacy_sdk_client(handler, max_retries=3),
}


@pytest.mark.parametrize("sdk", list(RETRYING_SDK_CLIENTS))
@pytest.mark.parametrize(
    "create",
    [
        pytest.param(500, id="500"),
        pytest.param(502, id="502"),
        pytest.param(503, id="503"),
        pytest.param(408, id="408"),
        pytest.param(409, id="409"),
        pytest.param(429, id="429"),
        pytest.param(httpx2.ReadTimeout, id="timed out"),
        pytest.param(httpx2.ConnectError, id="connection error"),
    ],
)
def test_create_batch_sends_one_request_where_the_sdk_would_retry(sdk, create):
    """Creating a job isn't idempotent: an SDK retry after a lost response would start a second, billed job."""
    seen: list[httpx2.Request] = []
    sdk_client = RETRYING_SDK_CLIENTS[sdk](batch_service(seen, create=create))
    client = AzureChatClient(client=sdk_client, batch_deployment="gpt-batch")
    with pytest.raises(LLMRequestError) as caught:
        client.create_batch("file-1")
    assert caught.value.retryable
    assert calls(seen) == ["POST /batches", "GET /batches"]  # one try, then a look for the job it may have made
    # The client keeps its own retries, which the other Batch API calls go on using.
    assert sdk_client.max_retries == 3
    seen.clear()
    with pytest.raises(LLMRequestError):
        client.get_batch("batch-1")
    assert calls(seen) == ["GET /batches/{id}"] * (1 + 3)


@pytest.mark.parametrize("sdk", list(RETRYING_SDK_CLIENTS))
@pytest.mark.parametrize(
    "create", [500, httpx2.ReadTimeout, httpx2.RemoteProtocolError], ids=["500", "timeout", "drop"]
)
def test_create_batch_adopts_the_job_azure_started_although_the_call_failed(sdk, create, caplog):
    seen: list[httpx2.Request] = []
    listed = [batch_json("batch-9", "file-9"), batch_json("batch-ours", "file-1", "validating"), batch_json("b", "f")]
    client = AzureChatClient(
        client=RETRYING_SDK_CLIENTS[sdk](batch_service(seen, create=create, listed=listed)), batch_deployment="gpt"
    )
    with caplog.at_level(logging.INFO, logger="azure_mapreduce.client"):
        job = client.create_batch("file-1")
    assert job == BatchJob(id="batch-ours", status="validating", completed=1, failed=0, total=4)
    assert calls(seen) == ["POST /batches", "GET /batches"]
    assert seen[1].url.params["limit"] == "50"
    assert "Creating the batch job failed" in caplog.text and "carrying on with it" in caplog.text


@pytest.mark.parametrize(
    "listed",
    [
        pytest.param([], id="no jobs"),
        pytest.param([batch_json("batch-9", "file-9"), batch_json("batch-10", "file-10")], id="other files' jobs"),
        pytest.param([batch_json("batch-9", "file-10")], id="a file id that only starts the same"),
    ],
)
def test_create_batch_raises_the_create_error_when_no_job_uses_the_file(listed):
    seen: list[httpx2.Request] = []
    client = AzureChatClient(client=sdk_client(batch_service(seen, create=500, listed=listed)), batch_deployment="b")
    with pytest.raises(LLMRequestError, match="Azure broke") as caught:
        client.create_batch("file-1")
    assert (caught.value.retryable, caught.value.code) == (True, "server_error")
    assert type(caught.value.__cause__) is openai.InternalServerError
    assert calls(seen) == ["POST /batches", "GET /batches"]


@pytest.mark.parametrize("list_status", [500, 401, 403, 404, 400])
def test_create_batch_raises_the_create_error_when_the_job_list_fails_too(list_status):
    seen: list[httpx2.Request] = []
    client = AzureChatClient(
        client=sdk_client(batch_service(seen, create=httpx2.ReadTimeout, list_status=list_status)), batch_deployment="b"
    )
    with pytest.raises(LLMRequestError) as caught:
        client.create_batch("file-1")
    assert (caught.value.retryable, caught.value.code) == (True, "timeout")  # the create's error, not the list's
    assert type(caught.value.__cause__) is openai.APITimeoutError
    assert calls(seen) == ["POST /batches", "GET /batches"]


def test_create_batch_raises_the_create_error_when_the_job_list_cant_connect():
    seen: list[httpx2.Request] = []
    service = batch_service(seen, create=503)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            seen.append(request)
            raise httpx2.ConnectError("Connection refused", request=request)
        return service(request)

    client = AzureChatClient(client=sdk_client(handler), batch_deployment="b")
    with pytest.raises(LLMRequestError, match="Azure broke") as caught:
        client.create_batch("file-1")
    assert type(caught.value.__cause__) is openai.InternalServerError
    assert calls(seen) == ["POST /batches", "GET /batches"]


@pytest.mark.parametrize(
    ("create", "error"),
    [
        pytest.param((400, error_json("invalid_request", "not a batch file")), LLMRequestError, id="400 final"),
        pytest.param((400, error_json("content_filter")), LLMRequestError, id="400 content_filter"),
        pytest.param((401, error_json("invalid_api_key")), LLMSetupError, id="401"),
        pytest.param((403, error_json("PermissionDenied")), LLMSetupError, id="403"),
        pytest.param((404, error_json("404", "Resource not found")), LLMSetupError, id="404"),
        pytest.param((422, error_json("unprocessable")), LLMRequestError, id="422"),
        pytest.param((400, {"errors": [{"code": "model_not_found"}]}), LLMSetupError, id="400 batch setup code"),
    ],
)
def test_create_batch_doesnt_look_for_a_job_after_a_failure_that_isnt_passing(create, error):
    """Azure answered no: there's no job to find (and one listed on the file isn't adopted)."""
    seen: list[httpx2.Request] = []
    listed = [batch_json("batch-ours", "file-1")]
    client = AzureChatClient(client=sdk_client(batch_service(seen, create=create, listed=listed)), batch_deployment="b")
    with pytest.raises(error) as caught:
        client.create_batch("file-1")
    assert not getattr(caught.value, "retryable", False)
    assert calls(seen) == ["POST /batches"]


def test_create_batch_quota_rejection_stays_the_quota_error():
    seen: list[httpx2.Request] = []
    create = (400, error_json(QUOTA_CODE, "Enqueued token limit reached for gpt-batch"))
    client = AzureChatClient(
        client=sdk_client(batch_service(seen, create=create, listed=[batch_json("batch-9", "file-9")])),
        batch_deployment="gpt-batch",
    )
    with pytest.raises(LLMRequestError, match="enqueued-token quota is full") as caught:
        client.create_batch("file-1")
    assert (caught.value.retryable, caught.value.code) == (True, QUOTA_CODE)
    assert type(caught.value.__cause__) is openai.BadRequestError
    assert calls(seen).count("POST /batches") == 1


def test_create_batch_that_succeeds_doesnt_list_jobs():
    seen: list[httpx2.Request] = []
    client = AzureChatClient(client=RETRYING_SDK_CLIENTS["v1"](batch_service(seen)), batch_deployment="gpt-batch")
    assert client.create_batch("file-1") == BatchJob(id="batch-new", status="validating", completed=1, total=4)
    assert calls(seen) == ["POST /batches"]


class WrappedBatches:
    """The batches of a wrapped SDK that can't list jobs, whose job creation fails without an answer."""

    def __init__(self):
        self.created: list[dict] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        raise openai.APIConnectionError(request=httpx2.Request("POST", BASE_URL + "batches"))


class WrappedListingBatches(WrappedBatches):
    def __init__(self, listed: list[dict]):
        super().__init__()
        self.listed = listed
        self.listed_with: list[dict] = []

    def list(self, **kwargs):
        self.listed_with.append(kwargs)
        return SimpleNamespace(data=[openai.types.Batch.model_validate(batch) for batch in self.listed])


class WrappedClient:
    """A wrapped SDK client without with_options."""

    def __init__(self, batches: WrappedBatches):
        self.batches = batches
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: None))


def test_create_batch_on_a_wrapped_client_without_with_options():
    wrapped = WrappedClient(WrappedListingBatches([batch_json("batch-ours", "file-1")]))
    client = AzureChatClient(client=wrapped, batch_deployment="gpt-batch")
    assert client.create_batch("file-1").id == "batch-ours"
    assert wrapped.batches.created == [
        {
            "input_file_id": "file-1",
            "endpoint": BATCH_ENDPOINT,
            "completion_window": "24h",
            "output_expires_after": {"anchor": "created_at", "seconds": BATCH_FILE_EXPIRY_SECONDS},
        }
    ]
    assert wrapped.batches.listed_with == [{"limit": 50}]


def test_create_batch_on_a_wrapped_client_that_cant_list_jobs_raises_the_create_error():
    wrapped = WrappedClient(WrappedBatches())  # no batches.list at all
    client = AzureChatClient(client=wrapped, batch_deployment="gpt-batch")
    with pytest.raises(LLMRequestError, match="Couldn't connect") as caught:
        client.create_batch("file-1")
    assert caught.value.code == "connection"
    assert len(wrapped.batches.created) == 1


def test_create_batch_without_retries_still_signs_in_with_a_fresh_entra_id_token(fake_endpoint, entra):
    client = AzureChatClient("res", batch_deployment="gpt-batch")
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    client.get_batch(job.id)
    assert [request.headers["authorization"] for _, request in fake_endpoint.requests] == [
        "Bearer entra-token-1",
        "Bearer entra-token-2",
        "Bearer entra-token-3",
    ]
    assert [str(request.url) for request in fake_endpoint.sent("POST", "/batches")] == [BASE_URL + "batches"]
    assert entra == [FOUNDRY_SCOPE]
    assert client._client.max_retries == 6  # the client's own retries are left as they were


# --- create_batch: Azure's {"errors": ...} replies -------------------------------------------------------

ERRORS_SHAPES = {
    "errors list": lambda errors: {"errors": errors},
    "errors.data": lambda errors: {"errors": {"object": "list", "data": errors}},
}


@pytest.mark.parametrize("code", sorted(BATCH_SETUP_CODES))
@pytest.mark.parametrize("shape", list(ERRORS_SHAPES))
def test_create_batch_400_with_a_batch_setup_code_in_its_errors_is_a_setup_error(shape, code):
    """Azure turns down the job for a reason every job would hit (e.g. no such batch deployment)."""
    seen: list[httpx2.Request] = []
    body = ERRORS_SHAPES[shape]([{"code": code, "message": f"{code} for this job", "line": None}])
    client = AzureChatClient(client=sdk_client(batch_service(seen, create=(400, body))), batch_deployment="gpt-b")
    with pytest.raises(LLMSetupError, match=r"^Azure rejected the batch job, and would reject every one: ") as caught:
        client.create_batch("file-1")
    assert f"{code} for this job" in str(caught.value)
    assert type(caught.value.__cause__) is openai.BadRequestError
    assert caught.value.__cause__.code is None  # the SDK saw no code: it's in the errors
    assert calls(seen) == ["POST /batches"]


@pytest.mark.parametrize("shape", list(ERRORS_SHAPES))
@pytest.mark.parametrize(
    "errors",
    [
        pytest.param([{"code": QUOTA_CODE, "message": "Enqueued token limit reached"}], id="quota"),
        pytest.param(
            [{"code": "invalid_json_line", "message": "bad line", "line": 3}, {"code": QUOTA_CODE, "message": "full"}],
            id="quota and a setup code",  # the quota decides: a setup problem shows on the next try
        ),
    ],
)
def test_create_batch_400_with_token_limit_exceeded_in_its_errors_is_the_quota_error(shape, errors):
    seen: list[httpx2.Request] = []
    client = AzureChatClient(
        client=sdk_client(batch_service(seen, create=(400, ERRORS_SHAPES[shape](errors)))), batch_deployment="b"
    )
    with pytest.raises(LLMRequestError, match="^The Batch API's enqueued-token quota is full: ") as caught:
        client.create_batch("file-1")
    assert (caught.value.retryable, caught.value.code) == (True, QUOTA_CODE)
    assert type(caught.value.__cause__) is openai.BadRequestError


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"errors": [{"code": "brand_new_code", "message": "?"}]}, id="unknown code"),
        pytest.param({"errors": []}, id="empty list"),
        pytest.param({"errors": {"object": "list", "data": []}}, id="empty data"),
        pytest.param({"errors": None}, id="null"),
        pytest.param({"errors": "model_not_found"}, id="a string"),
        pytest.param({"errors": {"data": "model_not_found"}}, id="data a string"),
        pytest.param({"errors": {"model_not_found": True}}, id="an object without data"),
        pytest.param({"errors": [None, 3, "model_not_found", {"code": 5}, {"message": "no code"}]}, id="odd items"),
    ],
)
def test_create_batch_400_without_a_known_code_in_its_errors_is_a_final_request_error(body):
    client = AzureChatClient(client=sdk_client(batch_service([], create=(400, body))), batch_deployment="b")
    with pytest.raises(LLMRequestError, match="^Azure rejected the request: ") as caught:
        client.create_batch("file-1")
    assert (caught.value.retryable, caught.value.code) == (False, None)


def test_the_error_code_the_sdk_found_wins_over_the_errors_list():
    body = {"code": "invalid_prompt", "message": "m", "errors": [{"code": "model_not_found"}]}
    client = AzureChatClient(client=sdk_client(batch_service([], create=(400, body))), batch_deployment="b")
    with pytest.raises(LLMRequestError) as caught:
        client.create_batch("file-1")
    assert (caught.value.retryable, caught.value.code) == (False, "invalid_prompt")


def test_upload_batch_file_400_with_a_setup_code_in_its_errors_is_a_setup_error():
    body = {"errors": {"object": "list", "data": [{"code": "invalid_json_line", "message": "Line 1", "line": 1}]}}
    client = client_on(responding(400, body))
    with pytest.raises(LLMSetupError, match="Azure rejected the batch job"):
        client.upload_batch_file(b"not json\n")


def test_chat_calls_dont_read_the_batch_errors_list():
    """The {"errors": ...} shape is the Batch API's; a chat call with it is just a rejected request."""
    client = client_on(responding(400, {"errors": [{"code": "model_not_found", "message": "x"}]}))
    with pytest.raises(LLMRequestError, match="^Azure rejected the request: ") as caught:
        client.complete(MESSAGES)
    assert (caught.value.retryable, caught.value.code) == (False, None)
    for code in ("unsupported_value", QUOTA_CODE):  # neither a setup error nor the quota: no batch=True
        error = translate_error(
            openai.BadRequestError(
                "Error code: 400",
                response=httpx2.Response(400, request=httpx2.Request("POST", BASE_URL + "chat/completions")),
                body={"errors": [{"code": code}]},
            ),
            "gpt-test",
        )
        assert type(error) is LLMRequestError
        assert (error.retryable, error.code) == (False, None)


def test_batch_setup_codes_live_in_the_client():
    from azure_mapreduce import runners

    assert runners.BATCH_SETUP_CODES is BATCH_SETUP_CODES
    assert {"model_not_found", "url_mismatch", "invalid_json_line", "DeploymentNotFound"} <= BATCH_SETUP_CODES
    assert QUOTA_CODE not in BATCH_SETUP_CODES  # a full quota is passing
    assert "server_error" not in BATCH_SETUP_CODES


def test_get_batch_follows_the_job_to_its_output_file():
    fake = FakeAzure(batch_statuses=("in_progress", "completed"))
    client = fake.client()
    lines = "".join(client.batch_line(f"request-{i}", MESSAGES) + "\n" for i in range(4))
    job = client.create_batch(client.upload_batch_file(lines.encode()))

    running = client.get_batch(job.id)
    assert (running.status, running.completed, running.total, running.output_file_id) == ("in_progress", 2, 4, None)
    done = client.get_batch(job.id)
    assert (done.status, done.completed, done.failed, done.total) == ("completed", 4, 0, 4)
    assert done.output_file_id is not None and done.error_file_id is None
    assert [str(request.url) for request in fake.sent("GET", "/batches/{id}")] == [BASE_URL + "batches/batch-1"] * 2

    output = [json.loads(raw) for raw in client.read_file(done.output_file_id).splitlines()]
    assert sorted(line["custom_id"] for line in output) == [f"request-{i}" for i in range(4)]
    assert all(batch_line_result(line) == "<Hello>" for line in output)


def test_cancel_batch_posts_to_the_cancel_path():
    fake = FakeAzure(batch_statuses=("in_progress",))
    client = fake.client()
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    cancelling = client.cancel_batch(job.id)
    (request,) = fake.sent("POST", "/batches/{id}/cancel")
    assert str(request.url) == BASE_URL + "batches/batch-1/cancel"
    assert request.content in (b"", b"{}")
    assert cancelling.status == "cancelling"
    assert client.get_batch(job.id).status == "cancelled"


def test_read_file_returns_the_file_content_as_text():
    fake = FakeAzure()
    client = fake.client()
    content = '{"custom_id": "request-0", "text": "Café — naïve"}\n{"custom_id": "request-1"}'
    file_id = fake.add_file(content.encode("utf-8"))
    assert client.read_file(file_id) == content
    (request,) = fake.sent("GET", "/files/{id}/content")
    assert str(request.url) == BASE_URL + f"files/{file_id}/content"


def test_delete_file_sends_a_delete():
    fake = FakeAzure()
    client = fake.client()
    file_id = fake.add_file(b"x")
    assert client.delete_file(file_id) is None
    (request,) = fake.sent("DELETE", "/files/{id}")
    assert str(request.url) == BASE_URL + f"files/{file_id}"
    assert fake.deleted == [file_id]


def test_batch_job_from_sdk_maps_counts_files_and_errors():
    batch = openai.types.Batch.model_validate(
        {
            "id": "batch-9",
            "object": "batch",
            "endpoint": "/chat/completions",
            "input_file_id": "file-1",
            "completion_window": "24h",
            "status": "failed",
            "created_at": 1,
            "output_file_id": "file-2",
            "error_file_id": "file-3",
            "request_counts": {"total": 5, "completed": 3, "failed": 2},
            "errors": {
                "object": "list",
                "data": [
                    {"code": "invalid_json_line", "message": "bad JSON", "line": 3},
                    {"code": None, "message": "model not found", "line": None},
                    {"code": "empty_file"},
                    {"message": "no code", "line": 0},
                ],
            },
        }
    )
    assert BatchJob.from_sdk(batch) == BatchJob(
        id="batch-9",
        status="failed",
        completed=3,
        failed=2,
        total=5,
        output_file_id="file-2",
        error_file_id="file-3",
        errors=(
            "invalid_json_line: bad JSON (line 3)",
            "model not found",
            "empty_file: no details",
            "no code (line 0)",
        ),
        error_codes=("invalid_json_line", "empty_file"),  # errors without a code add none
    )


def test_batch_job_from_sdk_without_counts_or_errors():
    batch = openai.types.Batch.model_validate(
        {
            "id": "batch-1",
            "object": "batch",
            "endpoint": "/chat/completions",
            "input_file_id": "file-1",
            "completion_window": "24h",
            "status": "validating",
            "created_at": 1,
        }
    )
    assert BatchJob.from_sdk(batch) == BatchJob(id="batch-1", status="validating")


def test_failed_batch_job_errors_come_through_the_sdk():
    fake = FakeAzure(
        batch_statuses=("failed",),
        batch_errors=({"code": "model_not_found", "message": "No batch deployment gpt-batch", "line": None},),
    )
    client = fake.client()
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    failed = client.get_batch(job.id)
    assert failed.status == "failed"
    assert failed.errors == ("model_not_found: No batch deployment gpt-batch",)
    assert failed.error_codes == ("model_not_found",)
    assert failed.output_file_id is None


def test_batch_job_error_codes_are_empty_unless_the_job_failed():
    fake = FakeAzure(batch_statuses=("completed",))
    client = fake.client()
    job = client.create_batch(client.upload_batch_file((client.batch_line("request-0", MESSAGES) + "\n").encode()))
    assert job.error_codes == ()
    done = client.get_batch(job.id)
    assert (done.status, done.errors, done.error_codes) == ("completed", (), ())


# --- batch_line_result -----------------------------------------------------------------------------------


def output_line(status: int | None = 200, body: Any = None, error: Any = None) -> dict:
    return {
        "id": "batch_req_1",
        "custom_id": "request-0",
        "response": None if status is None else {"status_code": status, "request_id": "r", "body": body},
        "error": error,
    }


def test_batch_line_result_success():
    assert batch_line_result(output_line(200, chat_body("  a reply  "))) == "a reply"


def test_batch_line_result_body_as_a_json_string():
    assert batch_line_result(output_line(200, json.dumps(chat_body("from a string")))) == "from a string"


def test_batch_line_result_body_as_a_json_string_with_an_error():
    outcome = batch_line_result(output_line(400, json.dumps(error_json("content_filter", "flagged"))))
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.retryable, outcome.code) == (False, "content_filter")


def test_batch_line_result_unreadable_body_string_is_retryable():
    outcome = batch_line_result(output_line(200, "<html>bad gateway</html>"))
    assert isinstance(outcome, LLMRequestError) and outcome.retryable


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("server_error", True),
        ("batch_expired", True),
        ("content_filter", False),
        ("ResponsibleAIPolicyViolation", False),
        ("context_length_exceeded", False),
        (None, True),
    ],
)
def test_batch_line_result_error_field(code, retryable):
    outcome = batch_line_result(output_line(None, error={"code": code, "message": "went wrong"}))
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.retryable, outcome.code) == (retryable, code)
    assert "went wrong" in str(outcome)


def test_batch_line_result_error_field_without_a_message():
    outcome = batch_line_result(output_line(None, error={"code": "server_error"}))
    assert isinstance(outcome, LLMRequestError)
    assert str(outcome) == "server_error: no details"


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "content_filter", False),
        (400, "context_length_exceeded", False),
        (429, "429", True),
        (500, "server_error", True),
        (400, "unsupported_parameter", True),  # the standard deployment may still take it
        (404, "DeploymentNotFound", True),
    ],
)
def test_batch_line_result_non_200_with_an_error_body(status, code, retryable):
    outcome = batch_line_result(output_line(status, error_json(code, "details here")))
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.retryable, outcome.code) == (retryable, code)
    assert "details here" in str(outcome)


def test_batch_line_result_content_filter_message():
    outcome = batch_line_result(output_line(400, error_json("content_filter", "hate: high")))
    assert str(outcome) == "Azure's content filter blocked this text: hate: high"


@pytest.mark.parametrize("body", [None, {}, "", {"error": None}])
def test_batch_line_result_non_200_without_details(body):
    outcome = batch_line_result(output_line(500, body))
    assert isinstance(outcome, LLMRequestError)
    assert outcome.retryable and outcome.code is None
    assert "status 500" in str(outcome)


def test_batch_line_result_no_response_and_no_error_is_retryable():
    outcome = batch_line_result({"custom_id": "request-0"})
    assert isinstance(outcome, LLMRequestError) and outcome.retryable


def test_batch_line_result_200_with_a_content_filtered_reply():
    outcome = batch_line_result(output_line(200, chat_body(None, finish_reason="content_filter")))
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.retryable, outcome.code) == (False, "content_filter")


def test_batch_line_result_200_with_an_empty_reply():
    outcome = batch_line_result(output_line(200, chat_body("")))
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.retryable, outcome.code) == (True, "empty")


@pytest.mark.parametrize(
    ("code", "expected"),
    [(429, "429"), (500, "500"), (0, "0"), (400.0, "400.0"), ("server_error", "server_error"), (None, None)],
)
@pytest.mark.parametrize("where", ["error field", "response body"])
def test_batch_line_result_error_codes_are_strings(code, expected, where):
    error = {"code": code, "message": "went wrong"}
    line = output_line(None, error=error) if where == "error field" else output_line(500, {"error": error})
    outcome = batch_line_result(line)
    assert isinstance(outcome, LLMRequestError)
    assert outcome.code == expected
    assert outcome.retryable
    assert str(outcome) == (f"{expected}: went wrong" if expected is not None else "went wrong")


WRAPPED_FILTER_ERROR = {"error": {"code": "content_filter", "message": "hate: high", "param": None, "type": None}}


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(WRAPPED_FILTER_ERROR, id="object"),
        pytest.param(json.dumps(WRAPPED_FILTER_ERROR), id="JSON string"),
        pytest.param(" \n\t" + json.dumps(WRAPPED_FILTER_ERROR) + "\n", id="JSON string with whitespace"),
        pytest.param(WRAPPED_FILTER_ERROR["error"], id="object without the error wrapper"),
        pytest.param(json.dumps(WRAPPED_FILTER_ERROR["error"]), id="JSON string without the error wrapper"),
        pytest.param(
            json.dumps({"error": {"code": None, "message": json.dumps(WRAPPED_FILTER_ERROR)}}), id="wrapped twice"
        ),
    ],
)
@pytest.mark.parametrize("where", ["error field", "response body", "response body as a string"])
def test_batch_line_result_unwraps_an_error_hidden_in_the_message(message, where):
    """Some gateways put the whole error object in the message and leave the code null."""
    error = {"code": None, "message": message}
    line = {
        "error field": output_line(None, error=error),
        "response body": output_line(400, {"error": error}),
        "response body as a string": output_line(400, json.dumps({"error": error})),
    }[where]
    outcome = batch_line_result(line)
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.retryable, outcome.code) == (False, "content_filter")  # not sent again: it's the text
    assert str(outcome) == "Azure's content filter blocked this text: hate: high"


def test_batch_line_result_unwrapped_passing_error_stays_retryable():
    error = {"code": None, "message": json.dumps({"error": {"code": "server_error", "message": "try later"}})}
    outcome = batch_line_result(output_line(None, error=error))
    assert (outcome.retryable, outcome.code, str(outcome)) == (True, "server_error", "server_error: try later")
    no_message = {"code": None, "message": {"error": {"code": 503}}}
    outcome = batch_line_result(output_line(None, error=no_message))
    assert (outcome.retryable, outcome.code, str(outcome)) == (True, "503", "503: no details")


@pytest.mark.parametrize(
    ("error", "code", "text"),
    [
        pytest.param(
            {"code": "server_error", "message": json.dumps(WRAPPED_FILTER_ERROR)},
            "server_error",
            "server_error: " + json.dumps(WRAPPED_FILTER_ERROR),
            id="a code of its own: the message is left as it is",
        ),
        pytest.param({"code": None, "message": "{not json"}, None, "{not json", id="not JSON after all"),
        pytest.param({"code": None, "message": '{"error": '}, None, '{"error": ', id="cut-off JSON"),
        pytest.param({"code": None, "message": "[1, 2]"}, None, "[1, 2]", id="a JSON list"),
        pytest.param({"code": None, "message": "  plain words "}, None, "  plain words ", id="plain text"),
        pytest.param({"code": None, "message": {"error": "a plain string"}}, None, "a plain string", id="string error"),
    ],
)
def test_batch_line_result_messages_that_arent_a_wrapped_error(error, code, text):
    outcome = batch_line_result(output_line(None, error=error))
    assert isinstance(outcome, LLMRequestError)
    assert (outcome.code, str(outcome)) == (code, text)
    assert outcome.retryable


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(json.dumps({"detail": "The gateway timed out"}), id="JSON string of another shape"),
        pytest.param({"detail": "The gateway timed out"}, id="object of another shape"),
        pytest.param({"error": None, "detail": "The gateway timed out"}, id="null error"),
    ],
)
def test_batch_line_result_keeps_a_json_message_it_cant_unwrap(message):
    outcome = batch_line_result(output_line(None, error={"code": None, "message": message}))
    assert isinstance(outcome, LLMRequestError) and outcome.retryable
    assert "The gateway timed out" in str(outcome)


# --- end to end: MapReduce through the real SDK against the fake Azure service ---------------------------


def test_end_to_end_batch_api(clock):
    """Every map record and reduce group goes through Batch API jobs: upload, validate, run, download, clean up."""
    fake = FakeAzure()
    df = reviews(7)
    result = mapreduce(fake.client(completion_options={"temperature": 0}), clock).run(df, "review", "summary")

    assert result.frame["summary"].tolist() == SEVEN_MAPPED
    assert result.frame["review"].tolist() == df["review"].tolist()
    assert "summary" not in df.columns  # the input is left alone
    assert result.levels == [SEVEN_MAPPED, SEVEN_LEVEL_1, [SEVEN_OUTPUT]]
    assert result.output == SEVEN_OUTPUT
    assert result.map_failures == {}

    assert fake.chat_calls == []  # nothing needed the fallbacks
    assert len(fake.batches) == 3 + 1 + 1  # map: jobs of 3, 3 and 1 records; each reduce level fits in one job
    assert all(batch["endpoint"] == "/v1/chat/completions" for batch in fake.batches.values())
    assert all(line["url"] == "/v1/chat/completions" and line["method"] == "POST" for line in fake.batch_lines)
    assert {line["body"]["model"] for line in fake.batch_lines} == {"gpt-batch"}
    assert {line["body"]["temperature"] for line in fake.batch_lines} == {0}
    assert len(fake.batch_lines) == 7 + 3 + 1
    uploads = fake.uploads()
    assert len(uploads) == 5
    assert all(fake.files[fid]["filename"].endswith(".jsonl") for fid in uploads)
    # Every input file and every job's output files were set to expire, in case the cleanup never happens.
    expiry = {"anchor": "created_at", "seconds": 1209600}
    assert all(fake.files[fid]["expires_after"] == expiry for fid in uploads)
    assert all(batch["output_expires_after"] == expiry for batch in fake.batches.values())
    assert sorted(fake.deleted) == sorted(fake.files)  # inputs and outputs are cleaned up
    # Each upload was checked until processed before its job was created.
    assert all(fake.files[fid]["retrieves"] == 2 for fid in uploads)
    assert clock.now > 0


def test_end_to_end_batch_api_keeps_files_without_cleanup(clock):
    fake = FakeAzure()
    result = mapreduce(fake.client(), clock, batch_cleanup=False).run(reviews(4), "review", "summary")
    assert result.frame["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake.deleted == []


def test_end_to_end_map_then_reduce_separately(clock):
    fake = FakeAzure()
    mr = mapreduce(fake.client(), clock)
    mapped = mr.map(reviews(7), "review", "summary")
    assert mapped["summary"].tolist() == SEVEN_MAPPED
    reduced = mr.reduce(mapped, column="summary")
    assert reduced.output == SEVEN_OUTPUT
    assert reduced.depth == 2
    assert fake.chat_calls == []


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param({"fail": {"POST /batches": (404, "DeploymentNotFound")}}, id="batch create 404"),
        pytest.param({"fail": {"POST /files": (400, "invalidPayload")}}, id="upload rejected"),
        pytest.param({"file_statuses": ("pending", "error")}, id="input file failed processing"),
    ],
)
def test_end_to_end_batch_api_unusable_falls_back_to_async(clock, setup):
    fake = FakeAzure(**setup)
    result = mapreduce(fake.client(), clock).run(reviews(7), "review", "summary")

    assert result.frame["summary"].tolist() == SEVEN_MAPPED
    assert result.levels == [SEVEN_MAPPED, SEVEN_LEVEL_1, [SEVEN_OUTPUT]]
    assert result.output == SEVEN_OUTPUT
    # All 7 + 3 + 1 requests went through the async client on the standard deployment.
    assert [(tag, model) for tag, model, _ in fake.chat_calls] == [("async", "gpt-test")] * 11
    assert sorted(prompt for _, _, prompt in fake.chat_calls[:7]) == [f"Summarize: r{i}" for i in range(7)]
    # The Batch API was tried once, then skipped for the rest of the run (both reduce levels too).
    assert len(fake.sent("POST", "/files")) == 1
    assert fake.batches == {}
    assert sorted(fake.deleted) == sorted(fake.files)  # a rejected input file isn't left behind
    assert fake.async_clients and all(client.is_closed() for client in fake.async_clients)


@pytest.mark.parametrize(
    ("code", "retired"),
    [
        pytest.param("model_not_found", True, id="setup failure retires the Batch API"),
        pytest.param("server_error", False, id="other failure is retried each step"),
    ],
)
def test_end_to_end_batch_then_async_then_sync(clock, code, retired):
    """A failed batch job hands its records to async calls; throttled async calls hand them to the loop."""
    fake = FakeAzure(
        batch_statuses=("validating", "failed"),
        batch_errors=({"code": code, "message": "The batch job failed"},),
        fail={"async POST /chat/completions": (429, "429")},
    )
    df = reviews(5)
    result = mapreduce(fake.client(), clock).run(df, "review", "summary")

    assert result.frame["summary"].tolist() == [f"S(r{i})" for i in range(5)]
    expected_level_1 = ["C(S(r0)+S(r1)+S(r2))", "C(S(r3)+S(r4))"]
    assert result.output == "C(" + "+".join(expected_level_1) + ")"
    assert [len(level) for level in result.levels] == [5, 2, 1]
    assert result.complete

    async_attempts = [request for tag, request in fake.requests if tag == "async"]
    assert len(async_attempts) == 5 + 2 + 1  # every request was tried with async first ...
    sync_calls = [(model, prompt) for tag, model, prompt in fake.chat_calls if tag == "sync"]
    assert len(sync_calls) == 5 + 2 + 1  # ... and answered by the loop
    assert {model for model, _ in sync_calls} == {"gpt-test"}
    if retired:
        # A job that failed for a setup reason (every job would) retires the Batch API for the rest of the run:
        # the map step's two jobs ran at once and failed validation together, and the reduce skipped it.
        assert len(fake.batches) == 2
        assert [batch["status"] for batch in fake.batches.values()] == ["failed", "failed"]
        assert fake.sent("POST", "/batches/{id}/cancel") == []
    else:
        # Any other failed job doesn't retire the Batch API: each step tried it again.
        assert len(fake.batches) == 2 + 1 + 1
        assert all(batch["status"] == "failed" for batch in fake.batches.values())
    assert sorted(fake.deleted) == sorted(fake.uploads())  # no input file left behind


def test_end_to_end_batch_endpoint_rejected_by_azure_falls_back_to_async(clock, caplog):
    """Lines for a path the v1 Batch API doesn't take fail validation (url_mismatch), and so would every job."""
    fake = FakeAzure()
    client = fake.client(batch_endpoint="/chat/completions")
    with caplog.at_level(logging.WARNING, logger="azure_mapreduce"):
        result = mapreduce(client, clock).run(reviews(7), "review", "summary")

    assert result.frame["summary"].tolist() == SEVEN_MAPPED
    assert result.output == SEVEN_OUTPUT
    assert [(tag, model) for tag, model, _ in fake.chat_calls] == [("async", "gpt-test")] * (7 + 3 + 1)
    # The map step's three jobs were submitted at once and failed validation together, which retired the Batch
    # API (nothing left running to cancel); the reduce levels didn't try it again.
    assert len(fake.batches) == 3
    assert all(batch["endpoint"] == "/chat/completions" for batch in fake.batches.values())
    assert fake.sent("POST", "/batches/{id}/cancel") == []
    assert fake.batch_lines == []  # nothing ran
    assert sorted(fake.deleted) == sorted(fake.uploads())
    assert "url_mismatch" in caplog.text


def test_end_to_end_batch_endpoint_override_for_a_service_on_another_path(clock):
    fake = FakeAzure(batch_url="/chat/completions")
    result = mapreduce(fake.client(batch_endpoint="/chat/completions"), clock).run(reviews(7), "review", "summary")
    assert result.output == SEVEN_OUTPUT
    assert fake.chat_calls == []
    assert {line["url"] for line in fake.batch_lines} == {"/chat/completions"}
    assert {batch["endpoint"] for batch in fake.batches.values()} == {"/chat/completions"}


def test_end_to_end_batch_file_expiry_none(clock):
    fake = FakeAzure()
    result = mapreduce(fake.client(batch_file_expiry=None), clock).run(reviews(4), "review", "summary")
    assert result.output == "C(C(S(r0)+S(r1)+S(r2))+C(S(r3)))"
    assert all(fake.files[fid]["expires_after"] is None for fid in fake.uploads())
    assert all("output_expires_after" not in batch for batch in fake.batches.values())
    assert sorted(fake.deleted) == sorted(fake.files)  # without an expiry, the cleanup is what removes them


def test_end_to_end_failed_batch_lines_fall_back_unless_the_content_was_blocked(clock):
    fake = FakeAzure(line_errors={"Summarize: r1": (500, "server_error"), "Summarize: r4": (400, "content_filter")})
    result = mapreduce(fake.client(), clock).run(reviews(6), "review", "summary", error_column="error")

    assert result.frame["summary"].tolist() == ["S(r0)", "S(r1)", "S(r2)", "S(r3)", None, "S(r5)"]
    errors = result.frame["error"].tolist()
    assert "content filter" in errors[4]
    assert errors[:4] + errors[5:] == [None] * 5
    assert list(result.map_failures) == [4]
    assert result.map_failures[4].code == "content_filter"
    # Only the server error was retried, on the standard deployment; the blocked text wasn't sent again.
    assert fake.chat_calls == [("async", "gpt-test", "Summarize: r1")]
    # The record that failed for good is left out of the reduce.
    assert result.levels[0] == ["S(r0)", "S(r1)", "S(r2)", "S(r3)", "S(r5)"]
    assert result.output == "C(C(S(r0)+S(r1)+S(r2))+C(S(r3)+S(r5)))"


@pytest.mark.parametrize(
    ("setup", "options"),
    [
        pytest.param({"batch_statuses": ("in_progress", "expired")}, {}, id="expired"),
        pytest.param({"batch_statuses": ("in_progress",)}, {"batch_timeout": 5.0}, id="timed out and cancelled"),
    ],
)
def test_end_to_end_unfinished_batch_job_keeps_its_replies_and_sends_the_rest_to_async(clock, setup, options):
    fake = FakeAzure(**setup)
    mapped = mapreduce(fake.client(), clock, map_batch_size=4, **options).map(reviews(4), "review", "summary")
    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert [line["custom_id"] for line in fake.batch_lines] == ["request-0", "request-1"]  # the half that finished
    assert fake.chat_calls == [("async", "gpt-test", "Summarize: r2"), ("async", "gpt-test", "Summarize: r3")]
    cancels = fake.sent("POST", "/batches/{id}/cancel")
    assert len(cancels) == (1 if "batch_timeout" in options else 0)


def test_end_to_end_output_download_failure_falls_back_to_async(clock, caplog):
    fake = FakeAzure(fail={"GET /files/{id}/content": (500, "server_error")})
    with caplog.at_level(logging.WARNING, logger="azure_mapreduce"):
        mapped = mapreduce(fake.client(), clock).map(reviews(4), "review", "summary")
    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert sorted(prompt for _, _, prompt in fake.chat_calls) == [f"Summarize: r{i}" for i in range(4)]
    # Each output file was tried three times (5 s, then 10 s apart) ...
    downloads = Counter(_file_id(request) for request in fake.sent("GET", "/files/{id}/content"))
    outputs = [batch["output_file_id"] for batch in fake.batches.values()]
    assert downloads == {file_id: 3 for file_id in outputs}
    assert Counter(clock.sleeps)[5.0] == 2 and Counter(clock.sleeps)[10.0] == 2
    # ... and the files that couldn't be read were kept (their replies are recoverable); the inputs were deleted.
    assert sorted(fake.deleted) == sorted(fake.uploads())
    assert not set(outputs) & set(fake.deleted)
    assert "couldn't be downloaded" in caplog.text


def test_end_to_end_output_download_that_recovers_keeps_the_replies(clock):
    fake = FakeAzure(fail={"GET /files/{id}/content": (500, "server_error")}, fail_times={"GET /files/{id}/content": 2})
    mapped = mapreduce(fake.client(), clock, map_batch_size=4).map(reviews(4), "review", "summary")
    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake.chat_calls == []  # the third attempt got the file
    assert len(fake.sent("GET", "/files/{id}/content")) == 3
    assert sorted(fake.deleted) == sorted(fake.files)


def test_end_to_end_batch_poll_errors_are_waited_out(clock):
    """get_batch failing now and then (translated to a retryable LLMRequestError) doesn't lose the job."""
    fake = FakeAzure(fail={"GET /batches/{id}": (500, "server_error")}, fail_times={"GET /batches/{id}": 3})
    mapped = mapreduce(fake.client(), clock, map_batch_size=4).map(reviews(4), "review", "summary")
    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake.chat_calls == []
    assert len(fake.sent("GET", "/batches/{id}")) == 3 + 2


def test_end_to_end_batch_quota_error_on_submit_waits_and_submits_again(clock):
    """A full enqueued-token quota (400 token_limit_exceeded from the real SDK) backs off instead of falling back."""
    fake = FakeAzure(fail={"POST /batches": (400, "token_limit_exceeded")}, fail_times={"POST /batches": 2})
    mapped = mapreduce(fake.client(), clock, map_batch_size=4).map(reviews(4), "review", "summary")
    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake.chat_calls == []
    assert len(fake.sent("POST", "/batches")) == 3
    assert len(fake.batches) == 1
    assert clock.sleeps.count(60.0) >= 1 and clock.sleeps.count(120.0) >= 1  # backing off 1, then 2 minutes
    assert len(fake.uploads()) == 3  # each attempt uploaded its input again ...
    assert sorted(fake.deleted) == sorted(fake.files)  # ... and the refused ones were deleted


@pytest.mark.parametrize("lose_as", [None, 500, 503], ids=["timed out", "500", "503"])
def test_end_to_end_a_job_created_despite_a_lost_answer_is_adopted_not_started_twice(
    fake_endpoint, clock, caplog, lose_as
):
    """Azure made the job but the answer never arrived: the SDK (6 retries on this client) mustn't send the
    create again, and the runner carries on with the job it finds on the input file."""
    fake_endpoint.lose = {"POST /batches": 1}
    fake_endpoint.lose_as = lose_as
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch", api_key="k")
    with caplog.at_level(logging.INFO, logger="azure_mapreduce"):
        mapped = mapreduce(client, clock, map_batch_size=4).map(reviews(4), "review", "summary")

    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake_endpoint.chat_calls == []  # the adopted job's replies were used
    assert len(fake_endpoint.sent("POST", "/batches")) == 1
    assert len(fake_endpoint.batches) == 1  # one job, not two billed ones
    (listing,) = fake_endpoint.sent("GET", "/batches")
    assert listing.url.params["limit"] == "50"
    assert len(fake_endpoint.uploads()) == 1
    assert not [delay for delay in clock.sleeps if delay >= 30]  # nothing to back off from
    assert sorted(fake_endpoint.deleted) == sorted(fake_endpoint.files)  # each file deleted, once
    assert "carrying on with it" in caplog.text


def test_end_to_end_a_create_that_azure_never_got_is_submitted_again(fake_endpoint, clock):
    fake_endpoint.fail = {"POST /batches": (500, "server_error")}
    fake_endpoint.fail_times = {"POST /batches": 1}
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch", api_key="k")
    mapped = mapreduce(client, clock, map_batch_size=4).map(reviews(4), "review", "summary")

    assert mapped["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake_endpoint.chat_calls == []
    # One request per attempt, although this client retries 500s: the first found no job, so the runner
    # uploaded the input again 30 s later and created the job then.
    assert len(fake_endpoint.sent("POST", "/batches")) == 2
    assert len(fake_endpoint.sent("GET", "/batches")) == 1
    assert len(fake_endpoint.batches) == 1
    assert len(fake_endpoint.uploads()) == 2
    assert 30.0 in clock.sleeps
    assert sorted(fake_endpoint.deleted) == sorted(fake_endpoint.files)


def test_end_to_end_batch_api_through_a_legacy_azure_openai_client(clock):
    fake = FakeAzure(batch_url="/chat/completions")  # the classic API's Batch API
    client = AzureChatClient(client=fake.legacy_sdk_client(), batch_deployment="gpt-batch")
    result = mapreduce(client, clock).run(reviews(7), "review", "summary")

    assert result.output == SEVEN_OUTPUT
    assert fake.chat_calls == []
    assert len(fake.batches) == 3 + 1 + 1  # no job failed validation
    assert {line["url"] for line in fake.batch_lines} == {"/chat/completions"}
    assert {batch["endpoint"] for batch in fake.batches.values()} == {"/chat/completions"}
    assert all("output_expires_after" not in batch for batch in fake.batches.values())
    assert all(fake.files[file_id]["expires_after"] is None for file_id in fake.uploads())
    for _, request in fake.requests:
        assert request.url.path.startswith("/openai/") and not request.url.path.startswith("/openai/v1/")
        assert request.url.params["api-version"] == "2024-10-21"
        assert request.headers["api-key"] == "secret"
    assert sorted(fake.deleted) == sorted(fake.files)


def test_end_to_end_unicode_newlines_and_empty_records_survive_the_jsonl_round_trip(clock):
    fake = FakeAzure()
    texts = ["Café ☕ — naïve", 'line one\nline two "quoted" \\ backslash', None, "   ", "emoji 🎉 {json: [1]}"]
    mr = mapreduce(fake.client(), clock, system_prompt="Réponds en français.")
    mapped = mr.map(pd.DataFrame({"review": texts}), "review", "summary")

    assert mapped["summary"].tolist() == [f"S({texts[0]})", f"S({texts[1]})", None, None, f"S({texts[4]})"]
    system = {"role": "system", "content": "Réponds en français."}
    assert [line["body"]["messages"][0] for line in fake.batch_lines] == [system] * 3
    uploaded = fake.files["file-1"]["content"].decode("ascii")  # \u escapes: pure ASCII
    assert "Caf\\u00e9 \\u2615" in uploaded
    assert "\\ud83c\\udf89" in uploaded  # the emoji as a surrogate pair
    assert len(uploaded.splitlines()) == 3  # one line per request: newlines inside the texts are escaped
    # Azure reads the escapes back as the very same texts.
    sent = [line["body"]["messages"][-1]["content"] for line in fake.batch_lines]
    assert sent == [f"Summarize: {texts[i]}" for i in (0, 1, 4)]
    assert fake.chat_calls == []


def test_end_to_end_batch_files_are_split_by_the_size_of_the_escaped_text(clock, monkeypatch):
    """Each \u00e9 takes 6 bytes in the file, so a file holds fewer accented texts than their UTF-8 size suggests."""
    from azure_mapreduce import runners

    limit = 3000
    monkeypatch.setattr(runners, "MAX_BATCH_FILE_BYTES", limit)
    fake = FakeAzure()
    texts = [f"{i}" + "\u00e9" * 200 for i in range(6)]  # about 1.4 kB a line escaped, 0.6 kB in UTF-8
    mapped = mapreduce(fake.client(), clock, map_batch_size=100).map(pd.DataFrame({"review": texts}), "review", "s")

    assert mapped["s"].tolist() == [f"S({text})" for text in texts]
    contents = [fake.files[file_id]["content"] for file_id in fake.uploads()]
    assert all(content.isascii() and len(content) <= limit for content in contents)
    assert [len(content.splitlines()) for content in contents] == [2, 2, 2]
    assert fake.chat_calls == []


def test_end_to_end_bad_credentials_stop_the_run_with_a_setup_error(clock):
    fake = FakeAzure(fail={"*": (401, "invalid_api_key")})
    with pytest.raises(LLMSetupError, match="credentials"):
        mapreduce(fake.client(), clock).run(reviews(3), "review", "summary")
    tags = Counter(tag for tag, _ in fake.requests)
    assert tags["async"] >= 1 and tags["sync"] >= 2  # batch upload, then async, then the loop all tried


def test_end_to_end_bad_credentials_with_only_the_batch_api(clock):
    """README: the setup is wrong -> the last strategy stops the run with an LLMSetupError that says what to check."""
    fake = FakeAzure(fail={"*": (401, "invalid_api_key")})
    with pytest.raises(LLMSetupError, match="credentials") as caught:
        mapreduce(fake.client(), clock, strategies=("batch",)).run(reviews(3), "review", "summary")
    assert "Cognitive Services OpenAI Contributor" in str(caught.value)
    assert type(caught.value.__cause__) is openai.AuthenticationError
    assert caught.value.outputs == [None, None, None]
    assert len(fake.requests) == 1  # the upload was refused, so nothing else was tried


# --- end to end: Azure out of reach ----------------------------------------------------------------------


def test_end_to_end_one_dropped_connection_is_retried_by_the_next_strategy(clock):
    fake = FakeAzure(drop_prompts={"Summarize: r2": 1})
    client = fake.client(batch_deployment=None)
    result = mapreduce(client, clock).run(reviews(7), "review", "summary")

    assert result.frame["summary"].tolist() == SEVEN_MAPPED
    assert result.output == SEVEN_OUTPUT
    assert result.complete and result.map_failures == {}
    # The async attempt at r2 couldn't connect; the loop sent it again and everything else stayed async.
    assert [call for call in fake.chat_calls if call[0] == "sync"] == [("sync", "gpt-test", "Summarize: r2")]
    assert len([call for call in fake.chat_calls if call[0] == "async"]) == 6 + 3 + 1
    assert len(fake.requests) == 7 + 1 + 3 + 1


@pytest.mark.parametrize("strategy", ["sync", "async"])
def test_end_to_end_one_dropped_connection_on_the_last_strategy_fails_only_that_record(clock, strategy):
    fake = FakeAzure(drop_prompts={"Summarize: r2": 1})
    mr = mapreduce(fake.client(), clock, strategies=(strategy,))
    result = mr.run(reviews(7), "review", "summary", error_column="error")

    assert result.frame["summary"].tolist() == SEVEN_MAPPED[:2] + [None] + SEVEN_MAPPED[3:]
    assert list(result.map_failures) == [2]
    assert (result.map_failures[2].code, result.map_failures[2].retryable) == ("connection", True)
    assert "Couldn't connect" in result.frame["error"][2]
    assert not result.complete
    assert result.levels[0] == SEVEN_MAPPED[:2] + SEVEN_MAPPED[3:]


def test_end_to_end_two_dropped_connections_before_any_reply_dont_stop_the_strategy(clock):
    """The loop stops after 3 connection errors in a row before any success; 2 are just failed requests."""
    fake = FakeAzure(drop_prompts={"Summarize: r0": 1, "Summarize: r1": 1})
    result = mapreduce(fake.client(), clock, strategies=("sync", "async")).run(reviews(7), "review", "summary")

    assert result.output == SEVEN_OUTPUT
    assert result.complete
    retried = sorted(prompt for tag, _, prompt in fake.chat_calls if tag == "async")
    assert retried == ["Summarize: r0", "Summarize: r1"]
    # The loop kept going (it wasn't retired), so the reduce went through it too.
    assert [tag for tag, _, prompt in fake.chat_calls if prompt.startswith("Combine: ")] == ["sync"] * 4


@pytest.mark.parametrize(
    ("dropped", "stops"),
    [pytest.param(9, False, id="9 in a row after a reply"), pytest.param(10, True, id="10 in a row after a reply")],
)
def test_end_to_end_dropped_connections_after_a_reply(clock, dropped, stops):
    """Once Azure has answered, it takes 10 connection errors in a row to call it unreachable."""
    fake = FakeAzure(drop_prompts={f"Summarize: r{i}": 1 for i in range(1, dropped + 1)})
    mr = mapreduce(fake.client(), clock, strategies=("sync",), map_batch_size=100)
    if stops:
        with pytest.raises(LLMSetupError, match="Azure couldn't be reached on 10 attempts in a row") as caught:
            mr.run(reviews(12), "review", "summary")
        assert caught.value.outputs[0] == "S(r0)"  # the reply that arrived is kept
        assert len(fake.requests) == 1 + 10
    else:
        result = mr.run(reviews(12), "review", "summary")
        assert sorted(result.map_failures) == list(range(1, dropped + 1))
        assert all(error.code == "connection" for error in result.map_failures.values())
        assert result.levels[0] == ["S(r0)", "S(r10)", "S(r11)"]


@pytest.mark.parametrize(
    ("strategies", "attempts"),
    [
        pytest.param(("sync",), {"sync": 3}, id="sync"),
        pytest.param(("async",), {"async": 3}, id="async"),
        # The batch upload is a passing error to the batch runner: 4 tries (30, 60 and 120 s apart), then async.
        pytest.param(("batch", "async", "sync"), {"sync": 4 + 3, "async": 3}, id="all three"),
    ],
)
def test_end_to_end_azure_out_of_reach_stops_the_run_after_3_attempts(clock, strategies, attempts):
    """A wrong endpoint or no network: every request fails to connect, so each strategy gives up after 3."""
    fake = FakeAzure(disconnect={"*"})
    mr = mapreduce(fake.client(), clock, strategies=strategies, max_concurrency=1)
    with pytest.raises(LLMSetupError, match="Azure couldn't be reached on 3 attempts in a row") as caught:
        mr.run(reviews(7), "review", "summary")
    assert "Couldn't connect to Azure OpenAI" in str(caught.value)  # the last error says what to check
    assert caught.value.outputs == [None] * 7
    assert Counter(tag for tag, _ in fake.requests) == attempts
    if "batch" in strategies:
        # Every upload couldn't connect; after the last submit retry, on to async.
        assert len(fake.sent("POST", "/files")) == 1 + 3
        assert [delay for delay in clock.sleeps if delay >= 30] == [30.0, 60.0, 120.0]
        assert fake.batches == {}
    assert fake.chat_calls == []


def test_end_to_end_azure_out_of_reach_with_full_concurrency(clock):
    """With many requests in flight the async strategy may send more than 3 before it stops, but not all."""
    fake = FakeAzure(disconnect={"*"})
    mr = mapreduce(fake.client(batch_deployment=None), clock, strategies=("async",), map_batch_size=50)
    with pytest.raises(LLMSetupError, match="couldn't be reached"):
        mr.run(reviews(50), "review", "summary")
    assert 3 <= len(fake.requests) < 50


@pytest.mark.parametrize(
    ("error", "words", "token_requests"),
    [
        # Each strategy stops at its first request: batch upload, async, sync.
        pytest.param(no_credential_worked, "No Entra ID credential worked", 1 + 1 + 1, id="no credential"),
        # A failed token request is retryable: the batch upload is tried 4 times (3 submit retries), then async
        # and sync each give up after 3 in a row.
        pytest.param(
            client_authentication_failed,
            "Azure couldn't be reached on 3 attempts in a row",
            4 + 3 + 3,
            id="token fails",
        ),
        pytest.param(
            credential_unavailable, "Azure couldn't be reached on 3 attempts in a row", 4 + 3 + 3, id="unavailable"
        ),
        pytest.param(
            service_request_error, "Azure couldn't be reached on 3 attempts in a row", 4 + 3 + 3, id="no token endpoint"
        ),
    ],
)
def test_end_to_end_entra_id_sign_in_failure_stops_the_run(
    fake_endpoint, failing_sign_in, clock, error, words, token_requests
):
    requested = failing_sign_in(error())
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch")
    with pytest.raises(LLMSetupError, match=words) as caught:
        mapreduce(client, clock, max_concurrency=1).run(reviews(5), "review", "summary")
    assert caught.value.outputs == [None] * 5
    assert fake_endpoint.requests == []  # no token, so nothing was sent
    assert requested == [FOUNDRY_SCOPE] * token_requests


def test_end_to_end_with_the_real_default_azure_credential_and_no_credential(
    fake_endpoint, no_working_credential, clock
):
    client = AzureChatClient("res", deployment="gpt-test", batch_deployment="gpt-batch")
    with pytest.raises(LLMSetupError, match="^No Entra ID credential worked") as caught:
        mapreduce(client, clock).run(reviews(5), "review", "summary")
    assert "DefaultAzureCredential failed to retrieve a token" in str(caught.value)
    assert caught.value.outputs == [None] * 5
    assert fake_endpoint.requests == []
    assert not [delay for delay in clock.sleeps if delay >= 30]  # a setup error isn't waited out


def test_end_to_end_batch_only_client_never_calls_chat_completions(clock):
    fake = FakeAzure()
    client = fake.client(deployment=None)
    result = mapreduce(client, clock).run(reviews(4), "review", "summary")
    assert result.frame["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert fake.chat_calls == []


def test_end_to_end_with_a_client_built_from_the_endpoint(fake_endpoint, clock):
    """The clients AzureChatClient builds itself: batch jobs and the async fallback both reach the v1 API."""
    fake_endpoint.fail = {"POST /batches": (404, "DeploymentNotFound")}
    client = AzureChatClient("my-resource", deployment="gpt-test", batch_deployment="gpt-batch", api_key="secret")
    result = mapreduce(client, clock).run(reviews(4), "review", "summary")
    assert result.frame["summary"].tolist() == [f"S(r{i})" for i in range(4)]
    assert {tag for tag, _, _ in fake_endpoint.chat_calls} == {"async"}
    for _, request in fake_endpoint.requests:
        assert str(request.url).startswith("https://my-resource.openai.azure.com/openai/v1/")
        assert request.headers["authorization"] == "Bearer secret"
    assert all(async_client.is_closed() for async_client in fake_endpoint.async_clients)


def test_end_to_end_async_inside_a_running_event_loop():
    """As in a Jupyter or Databricks notebook, where an event loop is already running."""
    fake = FakeAzure()
    mr = MapReduce(
        fake.client(),
        map_prompt="Summarize: {text}",
        reduce_prompt="Combine: {text}",
        strategies=("async",),
        show_progress=False,
    )

    async def notebook_cell():
        return mr.map_texts(["a", "b", None, "c"])

    assert asyncio.run(notebook_cell()) == ["S(a)", "S(b)", None, "S(c)"]
    assert {tag for tag, _, _ in fake.chat_calls} == {"async"}


def test_end_to_end_progress_bars(clock, capsys):
    fake = FakeAzure()
    mapreduce(fake.client(), clock, show_progress=True).run(reviews(7), "review", "summary")
    shown = capsys.readouterr().err
    assert "Map [batch]" in shown
    assert "jobs 3/3 done" in shown
    assert "Reduce" in shown
    assert "Level 1/2: 7 → 3 [batch]" in shown
    assert "Level 2/2: 3 → 1 [batch]" in shown
    assert "7/7" in shown and "2/2" in shown
