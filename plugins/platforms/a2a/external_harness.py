"""Strict, durable A2A 1.0 client for external agent harnesses.

This module owns only the outbound external-harness plane. It never imports or
invokes ACPX, ``delegate_task``, Kanban, Bot Mode, or Hermes peer orchestration.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol


PROTOCOL_VERSION = "1.0"
EXTENSION_URI = "urn:pysol-dev:a2a:extension:external-harness:v1"
_EXTENSION_PARAMS = {
    "contractVersion": "1.0",
    "capabilitiesMethod": "externalHarness.capabilities.get",
    "metadataField": "metadata.externalHarness",
    "eventCursorHeader": "Last-Event-ID",
}
_HARNESS_KINDS = frozenset({"opencode-omo", "codex", "claude", "remote-a2a"})
_TERMINAL_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
_INTERRUPTED_STATES = frozenset(
    {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
)
_NONTERMINAL_STATES = frozenset(
    {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
) | _INTERRUPTED_STATES
_ALL_STATES = _TERMINAL_STATES | _NONTERMINAL_STATES
_STRICT_TRANSITIONS = {
    "TASK_STATE_SUBMITTED": {
        "TASK_STATE_WORKING",
        "TASK_STATE_REJECTED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
    },
    "TASK_STATE_WORKING": {
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
    },
    "TASK_STATE_INPUT_REQUIRED": {
        "TASK_STATE_WORKING",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
    },
    "TASK_STATE_AUTH_REQUIRED": {
        "TASK_STATE_WORKING",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
    },
}
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ExternalHarnessError(RuntimeError):
    """Safe structured failure surfaced by the strict external path."""

    def __init__(
        self,
        reason_code: str,
        detail: str,
        *,
        retryable: bool = False,
        task_id: str | None = None,
        context_id: str | None = None,
        operation: str = "",
    ) -> None:
        self.reason_code = reason_code
        self.detail = _sanitize(detail)
        self.retryable = retryable
        self.task_id = task_id
        self.context_id = context_id
        self.operation = operation
        super().__init__(f"{reason_code}: {self.detail}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reasonCode": self.reason_code,
            "retryable": self.retryable,
            "taskId": self.task_id,
            "contextId": self.context_id,
            "operation": self.operation,
            "detail": self.detail,
        }


class A2ATransport(Protocol):
    def get_json(
        self, url: str, headers: dict[str, str]
    ) -> dict[str, Any]: ...

    def rpc(
        self,
        url: str,
        request: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]: ...

    def sse(
        self,
        url: str,
        request: dict[str, Any],
        headers: dict[str, str],
    ) -> Iterable[str]: ...


class StdlibA2ATransport:
    """Strict stdlib HTTP transport for the external-harness profile."""

    def __init__(self, timeout: float = 120) -> None:
        self.timeout = timeout

    def get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        return self._request_json(
            url, None, {"Accept": "application/json", **headers}
        )

    def rpc(
        self,
        url: str,
        request: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = self._request_json(
            url,
            request,
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                **headers,
            },
        )
        if response.get("jsonrpc") != "2.0" or "id" not in response:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "invalid JSON-RPC response envelope",
                retryable=True,
            )
        if response.get("id") != request.get("id"):
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "JSON-RPC correlation id mismatch",
                retryable=True,
            )
        has_result = "result" in response
        has_error = "error" in response
        if has_result == has_error:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "JSON-RPC response must contain exactly one result or error",
                retryable=True,
            )
        return response

    def sse(
        self,
        url: str,
        request: dict[str, Any],
        headers: dict[str, str],
    ) -> Iterable[str]:
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                **headers,
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(  # noqa: S310 - configured peer
                req, timeout=self.timeout
            )
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ExternalHarnessError(
                "UPSTREAM_UNREACHABLE", "peer stream connection failed", retryable=True
            ) from exc
        try:
            self._validate_response_headers(response.headers, headers)
        except Exception:
            response.close()
            raise

        def lines() -> Iterator[str]:
            try:
                with response:
                    for raw in response:
                        yield raw.decode("utf-8")
            except ExternalHarnessError:
                raise
            except (TimeoutError, urllib.error.URLError, OSError, UnicodeError) as exc:
                raise ExternalHarnessError(
                    "UPSTREAM_UNREACHABLE",
                    "peer stream disconnected",
                    retryable=True,
                    operation="subscribe",
                ) from exc

        return lines()

    def _request_json(
        self,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        data = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - configured peer
                req, timeout=self.timeout
            ) as response:
                self._validate_response_headers(response.headers, headers)
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ExternalHarnessError(
                "UPSTREAM_UNREACHABLE", "peer transport failed", retryable=True
            ) from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer returned malformed JSON",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer returned non-object JSON",
                retryable=True,
            )
        return payload

    @staticmethod
    def _validate_response_headers(response_headers: Any, request_headers: dict) -> None:
        if str(response_headers.get("A2A-Version") or "") != PROTOCOL_VERSION:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer omitted A2A-Version response header",
                retryable=True,
            )
        if request_headers.get("A2A-Extensions"):
            activated = {
                item.strip()
                for item in str(response_headers.get("A2A-Extensions") or "").split(",")
                if item.strip()
            }
            if EXTENSION_URI not in activated:
                raise ExternalHarnessError(
                    "CONTRACT_UNSUPPORTED",
                    "peer did not activate external harness extension",
                )


class ExternalHarnessClient:
    """A2A-only client with restart-safe SQLite correlation and event state."""

    REQUIRED_FEATURES = frozenset(
        {
            "durableTasks",
            "returnImmediately",
            "streaming",
            "replayableEvents",
            "followUp",
            "cancel",
            "restartRecovery",
            "strictIdentity",
            "runtimeIdentityProof",
            "hardWorkspaceIsolation",
        }
    )

    def __init__(
        self,
        peer_url: str,
        transport: A2ATransport,
        database: Path | str,
        token: str = "",
    ) -> None:
        self.peer_url = peer_url.rstrip("/")
        if not self.peer_url.startswith(("http://", "https://")):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "strict peer URL must use HTTP or HTTPS"
            )
        parsed_peer = urllib.parse.urlsplit(self.peer_url)
        if parsed_peer.username or parsed_peer.password:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "strict peer URL must not embed credentials",
            )
        if parsed_peer.scheme != "https" and parsed_peer.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "non-loopback strict peers require TLS",
            )
        if not token:
            raise ExternalHarnessError(
                "UPSTREAM_AUTH_REQUIRED",
                "strict external harness peers require bearer authentication",
            )
        self.transport = transport
        self.token = token
        self.storage_scope = (
            f"{self.peer_url}|bearer-sha256:"
            f"{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
        )
        database_path = Path(database)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db = sqlite3.connect(str(database_path), timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    peer_url TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    cursor INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(peer_url, task_id)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    peer_url TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(peer_url, operation, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    peer_url TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(peer_url, task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS peer_state (
                    peer_url TEXT PRIMARY KEY,
                    rpc_url TEXT NOT NULL,
                    tenant TEXT NOT NULL,
                    descriptor_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self.db.commit()
        except sqlite3.Error as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "external harness state store unavailable",
                retryable=True,
            ) from exc
        self.rpc_url = ""
        self.tenant = ""
        self._descriptor: dict[str, Any] | None = None

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "ExternalHarnessClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def cached_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            row = self.db.execute(
                "SELECT task_json FROM runs WHERE peer_url=? AND task_id=?",
                (self.storage_scope, task_id),
            ).fetchone()
            return json.loads(row["task_json"]) if row else None
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "external harness task state is unreadable",
                retryable=True,
                task_id=task_id,
                operation="get",
            ) from exc

    def start(
        self,
        harness_kind: str,
        mode_id: str,
        provider_id: str,
        model_id: str,
        workspace: dict[str, Any],
        prompt: str,
        idempotency_key: str,
        caller_correlation: str | None = None,
    ) -> dict[str, Any]:
        self._validate_start(
            harness_kind,
            mode_id,
            provider_id,
            model_id,
            workspace,
            prompt,
            idempotency_key,
        )
        body = {
            "harness_kind": harness_kind,
            "mode_id": mode_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "workspace": workspace,
            "prompt": prompt,
            "caller_correlation": caller_correlation,
        }
        existing, pending_request = self._idempotent(
            "start", idempotency_key, body
        )
        if existing is not None:
            return existing

        if pending_request is None:
            descriptor = self._preflight(
                harness_kind,
                mode_id,
                provider_id,
                model_id,
                workspace_strategy=str(workspace["strategy"]),
            )
            max_bytes = int((descriptor.get("limits") or {}).get("maxMessageBytes") or 0)
            if max_bytes and len(prompt.encode("utf-8")) > max_bytes:
                raise ExternalHarnessError(
                    "CONTRACT_UNSUPPORTED", "prompt exceeds advertised message limit"
                )
            metadata: dict[str, Any] = {
                "contractVersion": "1.0",
                "operation": "start",
                "request": {"idempotencyKey": idempotency_key},
                "harness": {"kind": harness_kind},
                "identity": {
                    "modeId": mode_id,
                    "model": {
                        "providerId": provider_id,
                        "modelId": model_id,
                    },
                },
                "workspace": workspace,
            }
            if caller_correlation:
                metadata["callerCorrelation"] = caller_correlation
            params: dict[str, Any] = {
                "message": {
                    "messageId": _new_ulid(),
                    "role": "ROLE_USER",
                    "parts": [{"text": prompt, "mediaType": "text/plain"}],
                    "extensions": [EXTENSION_URI],
                },
                "configuration": {
                    "acceptedOutputModes": ["text/plain", "application/json"],
                    "historyLength": 0,
                    "returnImmediately": True,
                },
                "metadata": {"externalHarness": metadata},
            }
            request = self._request("SendMessage", params)
            self._persist_intent("start", idempotency_key, body, request)
        else:
            self._ensure_interface()
            request = pending_request
            self._validate_stored_tenant(request)

        result = self._rpc_request(request, "start")
        task = result.get("task") if isinstance(result, dict) else None
        task = self._validate_task(task, "start")
        self._validate_start_binding(
            task,
            harness_kind=harness_kind,
            mode_id=mode_id,
            provider_id=provider_id,
            model_id=model_id,
            workspace=workspace,
        )
        self._persist_success("start", idempotency_key, task)
        return task

    def get(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "task id is required", operation="get"
            )
        task = self._result(
            "GetTask", {"id": task_id, "historyLength": 0}, "get"
        )
        task = self._validate_task(task, "get", task_id=task_id)
        self._store_task(task)
        return task

    def list(
        self,
        *,
        context_id: str = "",
        status: str = "",
        page_size: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {
            "pageSize": max(1, min(int(page_size), 100)),
            "historyLength": 0,
            "includeArtifacts": False,
        }
        if context_id:
            params["contextId"] = context_id
        if status:
            params["status"] = status
        validated: builtins.list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        while True:
            result = self._result("ListTasks", params, "list")
            tasks = result.get("tasks", []) if isinstance(result, dict) else None
            if not isinstance(tasks, list):
                raise ExternalHarnessError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "peer returned invalid task list",
                    retryable=True,
                    operation="list",
                )
            page = [self._validate_task(item, "list") for item in tasks]
            for item in page:
                self._store_task(item)
            validated.extend(page)
            next_token = result.get("nextPageToken") if isinstance(result, dict) else None
            if not next_token:
                return validated
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise ExternalHarnessError(
                    "UPSTREAM_PROTOCOL_ERROR",
                    "peer returned an invalid task page token",
                    retryable=True,
                    operation="list",
                )
            seen_tokens.add(next_token)
            params = {**params, "pageToken": next_token}

    def follow_up(
        self,
        task_id: str,
        context_id: str,
        text: str,
        idempotency_key: str,
        kind: str = "steer",
        interaction_id: str = "",
    ) -> dict[str, Any]:
        if not all((task_id, context_id, text)):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "follow-up fields are required",
                operation="followUp",
            )
        if kind not in {"answer", "steer"}:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "follow-up kind must be answer or steer",
                operation="followUp",
            )
        if kind == "answer" and not interaction_id:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "answer follow-up requires interaction id",
                operation="followUp",
            )
        cached = self.cached_task(task_id)
        if cached and cached.get("contextId") != context_id:
            raise ExternalHarnessError(
                "CONTEXT_BINDING_MISMATCH",
                "task and context do not match",
                task_id=task_id,
                context_id=context_id,
                operation="followUp",
            )
        body = {
            "task_id": task_id,
            "context_id": context_id,
            "text": text,
            "kind": kind,
            "interaction_id": interaction_id,
        }
        existing, pending_request = self._idempotent(
            "followUp", idempotency_key, body
        )
        if existing is not None:
            return existing
        if pending_request is None:
            self._ensure_interface()
            follow_up: dict[str, Any] = {"kind": kind}
            if interaction_id:
                follow_up["interactionId"] = interaction_id
            params = {
                "message": {
                    "messageId": _new_ulid(),
                    "taskId": task_id,
                    "contextId": context_id,
                    "role": "ROLE_USER",
                    "parts": [{"text": text, "mediaType": "text/plain"}],
                    "extensions": [EXTENSION_URI],
                },
                "metadata": {
                    "externalHarness": {
                        "contractVersion": "1.0",
                        "operation": "followUp",
                        "request": {"idempotencyKey": idempotency_key},
                        "followUp": follow_up,
                    }
                },
            }
            request = self._request("SendMessage", params)
            self._persist_intent("followUp", idempotency_key, body, request)
        else:
            self._ensure_interface()
            request = pending_request
            self._validate_stored_tenant(request)
        result = self._rpc_request(request, "followUp")
        task = result.get("task") if isinstance(result, dict) else None
        task = self._validate_task(task, "followUp", task_id=task_id)
        if task["id"] != task_id or task["contextId"] != context_id:
            raise ExternalHarnessError(
                "CONTEXT_BINDING_MISMATCH",
                "peer changed follow-up task or context",
                task_id=task_id,
                context_id=context_id,
                operation="followUp",
            )
        self._persist_success("followUp", idempotency_key, task)
        return task

    def cancel(self, task_id: str, reason: str = "") -> dict[str, Any]:
        if not task_id:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "task id is required", operation="cancel"
            )
        cached = self.cached_task(task_id)
        if cached and _task_state(cached) == "TASK_STATE_CANCELED":
            return cached
        params: dict[str, Any] = {"id": task_id}
        if reason:
            params["metadata"] = {"externalHarness": {"reason": reason}}
        task = self._result("CancelTask", params, "cancel")
        if isinstance(task, dict) and isinstance(task.get("task"), dict):
            task = task["task"]
        task = self._validate_task(task, "cancel", task_id=task_id)
        if _task_state(task) not in _TERMINAL_STATES:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "cancel returned a nonterminal task",
                retryable=True,
                task_id=task_id,
                context_id=task.get("contextId"),
                operation="cancel",
            )
        self._store_task(task)
        return task

    def subscribe(
        self, task_id: str, *, reconnect_attempts: int = 2
    ) -> Iterator[dict[str, Any]]:
        if not task_id:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "task id is required",
                operation="subscribe",
            )
        self._ensure_interface()
        attempts = 0
        while True:
            row = self.db.execute(
                "SELECT cursor FROM runs WHERE peer_url=? AND task_id=?",
                (self.storage_scope, task_id),
            ).fetchone()
            cursor = int(row["cursor"]) if row else 0
            needs_snapshot = cursor == 0
            stream_terminal = False
            headers = self._headers()
            if cursor:
                headers["Last-Event-ID"] = f"{task_id}:{cursor}"
            request = self._request("SubscribeToTask", {"id": task_id})
            try:
                frames = self.transport.sse(
                    self.rpc_url, request, headers
                )
                for sse_id, payload in self._parse_sse(frames, request["id"]):
                    if stream_terminal:
                        raise ExternalHarnessError(
                            "STREAM_CORRUPT",
                            "peer emitted an event after terminal status",
                            retryable=True,
                            task_id=task_id,
                            operation="subscribe",
                        )
                    if isinstance(payload.get("task"), dict):
                        snapshot = self._validate_task(
                            payload["task"], "subscribe", task_id=task_id
                        )
                        self._store_task(snapshot)
                        latest = _snapshot_latest_sequence(snapshot)
                        if needs_snapshot:
                            self._set_cursor(task_id, latest)
                            cursor = latest
                            needs_snapshot = False
                        continue
                    if needs_snapshot:
                        raise ExternalHarnessError(
                            "STREAM_CORRUPT",
                            "subscription omitted its initial task snapshot",
                            retryable=True,
                            task_id=task_id,
                            operation="subscribe",
                        )
                    stream = _event_stream_metadata(payload)
                    sequence = stream.get("sequence")
                    event_id = stream.get("eventId")
                    if (
                        not isinstance(sequence, int)
                        or sequence < 1
                        or event_id != f"{task_id}:{sequence}"
                        or sse_id != event_id
                    ):
                        raise ExternalHarnessError(
                            "STREAM_CORRUPT",
                            "invalid stream event identity",
                            retryable=True,
                            task_id=task_id,
                            operation="subscribe",
                        )
                    canonical = _canonical(payload)
                    if sequence <= cursor:
                        prior = self.db.execute(
                            "SELECT payload FROM events WHERE peer_url=? AND task_id=? AND sequence=?",
                            (self.storage_scope, task_id, sequence),
                        ).fetchone()
                        if prior and prior["payload"] != canonical:
                            raise ExternalHarnessError(
                                "STREAM_CORRUPT",
                                "non-equivalent duplicate event",
                                retryable=True,
                                task_id=task_id,
                                operation="subscribe",
                            )
                        continue
                    if sequence != cursor + 1:
                        raise ExternalHarnessError(
                            "STREAM_CORRUPT",
                            "stream sequence gap",
                            retryable=True,
                            task_id=task_id,
                            operation="subscribe",
                        )
                    updated = self._persist_event(
                        task_id, sequence, str(event_id), payload
                    )
                    cursor = sequence
                    stream_terminal = _is_terminal(updated)
                    yield payload
                return
            except ExternalHarnessError as exc:
                if exc.reason_code == "EVENT_CURSOR_EXPIRED":
                    snapshot = self.get(task_id)
                    raise ExternalHarnessError(
                        "EVENT_CURSOR_EXPIRED",
                        "event cursor expired; authoritative task snapshot refreshed but incremental reconstruction is unavailable",
                        retryable=False,
                        task_id=task_id,
                        context_id=snapshot.get("contextId"),
                        operation="subscribe",
                    ) from exc
                if not exc.retryable or attempts >= max(0, reconnect_attempts):
                    raise
                attempts += 1
                snapshot = self.get(task_id)
                if _task_state(snapshot) in _TERMINAL_STATES | _INTERRUPTED_STATES:
                    return

    def _preflight(
        self,
        harness_kind: str,
        mode_id: str,
        provider_id: str,
        model_id: str,
        *,
        workspace_strategy: str | None = None,
    ) -> dict[str, Any]:
        self._discover_interface()
        descriptor = self._result(
            "externalHarness.capabilities.get",
            {"harnessKind": harness_kind},
            "preflight",
        )
        if not isinstance(descriptor, dict):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "capability descriptor must be an object",
                operation="preflight",
            )
        self._validate_descriptor(descriptor)
        harness = next(
            (
                item
                for item in descriptor["harnesses"]
                if isinstance(item, dict) and item.get("kind") == harness_kind
            ),
            None,
        )
        if not isinstance(harness, dict):
            raise ExternalHarnessError(
                "CAPABILITY_UNSUPPORTED",
                "requested harness unavailable",
                operation="preflight",
            )
        if not (
            isinstance(harness.get("runtimeVersion"), str)
            and harness["runtimeVersion"]
            and isinstance(harness.get("modes"), list)
            and isinstance(harness.get("models"), list)
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "harness descriptor is incomplete",
                operation="preflight",
            )
        features = harness.get("features")
        if not isinstance(features, dict) or not all(
            features.get(feature) is True for feature in self.REQUIRED_FEATURES
        ):
            raise ExternalHarnessError(
                "CAPABILITY_UNSUPPORTED",
                "required harness features unavailable",
                operation="preflight",
            )
        if not any(
            isinstance(mode, dict) and mode.get("id") == mode_id
            for mode in harness.get("modes", [])
        ):
            raise ExternalHarnessError(
                "MODE_UNAVAILABLE",
                "requested mode unavailable",
                operation="preflight",
            )
        if not any(
            isinstance(model, dict)
            and model.get("providerId") == provider_id
            and model.get("modelId") == model_id
            and model.get("available") is True
            for model in harness.get("models", [])
        ):
            raise ExternalHarnessError(
                "MODEL_UNAVAILABLE",
                "requested model unavailable",
                operation="preflight",
            )
        if (
            workspace_strategy
            and workspace_strategy not in descriptor["workspace"]["strategies"]
        ):
            raise ExternalHarnessError(
                "WORKSPACE_ISOLATION_UNAVAILABLE",
                "requested workspace strategy is unavailable",
                operation="preflight",
            )
        self._descriptor = descriptor
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO peer_state(peer_url, rpc_url, tenant, descriptor_digest, updated_at) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(peer_url) DO UPDATE SET "
                    "rpc_url=excluded.rpc_url, tenant=excluded.tenant, "
                    "descriptor_digest=excluded.descriptor_digest, updated_at=excluded.updated_at",
                    (
                        self.storage_scope,
                        self.rpc_url,
                        self.tenant,
                        _digest(descriptor),
                        time.time(),
                    ),
                )
        except sqlite3.Error as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "could not persist capability selection",
                retryable=True,
                operation="preflight",
            ) from exc
        return descriptor

    def _discover_interface(self) -> None:
        public = self.transport.get_json(
            f"{self.peer_url}/.well-known/agent-card.json",
            self._public_headers(),
        )
        interface = self._validate_card(public, "public")
        self.rpc_url = interface["url"]
        self.tenant = str(interface.get("tenant") or "")
        extended = self.transport.get_json(
            f"{self.peer_url}/extendedAgentCard", self._headers()
        )
        extended_interface = self._validate_card(extended, "extended")
        if (
            extended_interface["url"] != self.rpc_url
            or str(extended_interface.get("tenant") or "") != self.tenant
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "public and extended Agent Cards disagree",
                operation="preflight",
            )

    def _ensure_interface(self) -> None:
        if not self.rpc_url:
            self._discover_interface()

    def _validate_card(self, card: Any, label: str) -> dict[str, Any]:
        if not isinstance(card, dict):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                f"{label} Agent Card is invalid",
                operation="preflight",
            )
        capabilities = card.get("capabilities")
        if (
            not isinstance(capabilities, dict)
            or capabilities.get("streaming") is not True
            or capabilities.get("extendedAgentCard") is not True
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                f"{label} card lacks required capabilities",
                operation="preflight",
            )
        extensions = [
            item
            for item in card.get("extensions", [])
            if isinstance(item, dict) and item.get("uri") == EXTENSION_URI
        ]
        extension_params = extensions[0].get("params") if len(extensions) == 1 else None
        if (
            len(extensions) != 1
            or extensions[0].get("required") is not False
            or not isinstance(extension_params, dict)
            or any(
                extension_params.get(key) != value
                for key, value in _EXTENSION_PARAMS.items()
            )
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                f"{label} card lacks external harness extension",
                operation="preflight",
            )
        schemes = card.get("securitySchemes")
        requirements = card.get("security")
        bearer_names = {
            name
            for name, scheme in (
                schemes.items() if isinstance(schemes, dict) else []
            )
            if isinstance(scheme, dict)
            and scheme.get("type") == "http"
            and str(scheme.get("scheme") or "").lower() == "bearer"
        }
        bearer_required = isinstance(requirements, list) and any(
            isinstance(requirement, dict)
            and any(name in requirement for name in bearer_names)
            for requirement in requirements
        )
        if not bearer_names or not bearer_required:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                f"{label} card does not declare bearer authentication",
                operation="preflight",
            )
        interfaces = [
            item
            for item in card.get("supportedInterfaces", [])
            if isinstance(item, dict)
            and item.get("protocolBinding") == "JSONRPC"
            and item.get("protocolVersion") == PROTOCOL_VERSION
            and isinstance(item.get("url"), str)
            and item["url"].startswith(("http://", "https://"))
        ]
        if not interfaces:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                f"{label} card has no A2A 1.0 JSONRPC interface",
                operation="preflight",
            )
        selected = interfaces[0]
        _validate_rpc_url(selected["url"])
        return selected

    def _validate_descriptor(self, descriptor: dict[str, Any]) -> None:
        if (
            descriptor.get("contractVersion") != "1.0"
            or descriptor.get("protocolVersion") != PROTOCOL_VERSION
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "capability contract mismatch",
                operation="preflight",
            )
        adapter = descriptor.get("adapter")
        if not isinstance(adapter, dict) or not all(
            isinstance(adapter.get(field), str) and adapter[field]
            for field in ("name", "version", "instanceId")
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "capability descriptor lacks adapter identity",
                operation="preflight",
            )
        if not _ULID_RE.fullmatch(adapter["instanceId"]):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "adapter instanceId must be a ULID",
                operation="preflight",
            )
        workspace = descriptor.get("workspace")
        if (
            not isinstance(workspace, dict)
            or workspace.get("isolation") != "hard"
            or not isinstance(workspace.get("strategies"), list)
        ):
            raise ExternalHarnessError(
                "CAPABILITY_UNSUPPORTED",
                "hard workspace isolation is required",
                operation="preflight",
            )
        limits = descriptor.get("limits")
        if not isinstance(limits, dict) or not all(
            isinstance(limits.get(field), int) and limits[field] > 0
            for field in (
                "maxConcurrentTasks",
                "maxMessageBytes",
                "cancelConfirmTimeoutMs",
                "taskRetentionSeconds",
                "eventRetentionSeconds",
            )
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "capability descriptor lacks required limits",
                operation="preflight",
            )
        if not isinstance(descriptor.get("harnesses"), list):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "capability descriptor lacks harness list",
                operation="preflight",
            )

    def _result(self, method: str, params: dict[str, Any], operation: str) -> Any:
        self._ensure_interface()
        return self._rpc_request(self._request(method, params), operation)

    def _rpc_request(self, request: dict[str, Any], operation: str) -> Any:
        try:
            response = self.transport.rpc(
                self.rpc_url or self.peer_url, request, self._headers()
            )
        except ExternalHarnessError as exc:
            if not exc.operation:
                exc.operation = operation
            raise
        if not isinstance(response, dict):
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "non-object peer response",
                retryable=True,
                operation=operation,
            )
        if "error" in response:
            error = response["error"]
            data = error.get("data", {}) if isinstance(error, dict) else {}
            raise ExternalHarnessError(
                str(data.get("reasonCode") or "UPSTREAM_PROTOCOL_ERROR"),
                str(data.get("detail") or "peer request failed"),
                retryable=bool(data.get("retryable", False)),
                task_id=data.get("taskId"),
                context_id=data.get("contextId"),
                operation=str(data.get("operation") or operation),
            )
        if "result" not in response:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer response omitted result",
                retryable=True,
                operation=operation,
            )
        return response["result"]

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.tenant and "tenant" not in params:
            params = {**params, "tenant": self.tenant}
        return {
            "jsonrpc": "2.0",
            "id": _new_ulid(),
            "method": method,
            "params": params,
        }

    def _public_headers(self) -> dict[str, str]:
        headers = {"A2A-Version": PROTOCOL_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _headers(self) -> dict[str, str]:
        return {**self._public_headers(), "A2A-Extensions": EXTENSION_URI}

    def _idempotent(
        self, operation: str, key: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        self._validate_idempotency_key(key, operation)
        digest = _digest(body)
        row = self.db.execute(
            "SELECT digest, request_json, response_json FROM requests "
            "WHERE peer_url=? AND operation=? AND idempotency_key=?",
            (self.storage_scope, operation, key),
        ).fetchone()
        if not row:
            return None, None
        if row["digest"] != digest:
            raise ExternalHarnessError(
                "IDEMPOTENCY_CONFLICT",
                "idempotency key was reused with different input",
                operation=operation,
            )
        if row["response_json"]:
            return json.loads(row["response_json"]), None
        return None, json.loads(row["request_json"])

    def _persist_intent(
        self,
        operation: str,
        key: str,
        body: dict[str, Any],
        request: dict[str, Any],
    ) -> None:
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO requests(peer_url, operation, idempotency_key, digest, request_json, response_json) "
                    "VALUES(?, ?, ?, ?, ?, '')",
                    (
                        self.storage_scope,
                        operation,
                        key,
                        _digest(body),
                        _canonical(request),
                    ),
                )
        except sqlite3.IntegrityError:
            existing, pending = self._idempotent(operation, key, body)
            if existing is not None or pending is not None:
                return
            raise
        except sqlite3.Error as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "could not persist request before remote dispatch",
                retryable=True,
                operation=operation,
            ) from exc

    def _persist_success(
        self, operation: str, key: str, task: dict[str, Any]
    ) -> None:
        try:
            with self.db:
                self._store_task(task, commit=False)
                self.db.execute(
                    "UPDATE requests SET response_json=? WHERE peer_url=? AND operation=? AND idempotency_key=?",
                    (_canonical(task), self.storage_scope, operation, key),
                )
        except sqlite3.Error as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "remote task accepted but local correlation could not be persisted",
                retryable=True,
                task_id=str(task.get("id") or "") or None,
                context_id=str(task.get("contextId") or "") or None,
                operation=operation,
            ) from exc

    def _store_task(
        self,
        task: dict[str, Any],
        *,
        commit: bool = True,
        strict_transition: bool = False,
    ) -> None:
        task = self._validate_task(task, "store")
        existing = self.db.execute(
            "SELECT task_json FROM runs WHERE peer_url=? AND task_id=?",
            (self.storage_scope, task["id"]),
        ).fetchone()
        if existing:
            prior = json.loads(existing["task_json"])
            if _is_terminal(prior):
                if _canonical(prior) != _canonical(task):
                    raise ExternalHarnessError(
                        "INVALID_TRANSITION",
                        "terminal task snapshot changed",
                        task_id=task["id"],
                        context_id=task["contextId"],
                        operation="store",
                    )
                return
            old_state = _task_state(prior)
            new_state = _task_state(task)
            if old_state != new_state:
                allowed = (
                    new_state in _STRICT_TRANSITIONS.get(old_state, set())
                    if strict_transition
                    else _snapshot_transition_is_reachable(old_state, new_state)
                )
                if not allowed:
                    raise ExternalHarnessError(
                        "INVALID_TRANSITION",
                        f"task state regressed from {old_state} to {new_state}",
                        task_id=task["id"],
                        context_id=task["contextId"],
                        operation="store",
                    )
            self._validate_immutable_binding(prior, task)
        self.db.execute(
            "INSERT INTO runs(peer_url, task_id, context_id, task_json) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(peer_url, task_id) DO UPDATE SET "
            "context_id=excluded.context_id, task_json=excluded.task_json",
            (
                self.storage_scope,
                task["id"],
                task["contextId"],
                _canonical(task),
            ),
        )
        if commit:
            self.db.commit()

    def _persist_event(
        self,
        task_id: str,
        sequence: int,
        event_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        updated = self._updated_task_for_event(task_id, payload)
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO events(peer_url, task_id, sequence, event_id, payload) VALUES(?, ?, ?, ?, ?)",
                    (
                        self.storage_scope,
                        task_id,
                        sequence,
                        event_id,
                        _canonical(payload),
                    ),
                )
                self._store_task(updated, commit=False, strict_transition=True)
                self.db.execute(
                    "UPDATE runs SET cursor=? WHERE peer_url=? AND task_id=?",
                    (sequence, self.storage_scope, task_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ExternalHarnessError(
                "STREAM_CORRUPT",
                "duplicate event sequence conflict",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            ) from exc
        except sqlite3.Error as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "could not persist stream event",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            ) from exc
        return updated

    def _set_cursor(self, task_id: str, sequence: int) -> None:
        try:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET cursor=? WHERE peer_url=? AND task_id=?",
                    (sequence, self.storage_scope, task_id),
                )
        except sqlite3.Error as exc:
            raise ExternalHarnessError(
                "STATE_STORE_UNAVAILABLE",
                "could not persist stream cursor",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            ) from exc

    def _updated_task_for_event(
        self, task_id: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        cached = self.cached_task(task_id)
        if not cached:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "stream event has no authoritative task snapshot",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            )
        if _is_terminal(cached):
            raise ExternalHarnessError(
                "STREAM_CORRUPT",
                "peer emitted an event after terminal status",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            )
        update_keys = [
            key
            for key in ("statusUpdate", "artifactUpdate")
            if isinstance(event.get(key), dict)
        ]
        if len(update_keys) != 1:
            raise ExternalHarnessError(
                "STREAM_CORRUPT",
                "stream response must contain exactly one task update",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            )
        update = event[update_keys[0]]
        if (
            update.get("taskId") != task_id
            or update.get("contextId") != cached.get("contextId")
        ):
            raise ExternalHarnessError(
                "STREAM_CORRUPT",
                "stream update task or context binding changed",
                retryable=True,
                task_id=task_id,
                operation="subscribe",
            )

        status_update = event.get("statusUpdate")
        if isinstance(status_update, dict) and isinstance(
            status_update.get("status"), dict
        ):
            cached["status"] = status_update["status"]
            metadata = status_update.get("metadata")
            if isinstance(metadata, dict):
                cached["metadata"] = _merge_dicts(
                    cached.get("metadata"), _snapshot_metadata_from_event(metadata)
                )

        artifact_update = event.get("artifactUpdate")
        if isinstance(artifact_update, dict) and isinstance(
            artifact_update.get("artifact"), dict
        ):
            artifacts = list(cached.get("artifacts") or [])
            artifact = artifact_update["artifact"]
            artifact_id = artifact.get("artifactId")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ExternalHarnessError(
                    "STREAM_CORRUPT",
                    "artifact update omitted artifactId",
                    retryable=True,
                    task_id=task_id,
                    operation="subscribe",
                )
            replaced = False
            for index, prior in enumerate(artifacts):
                if isinstance(prior, dict) and prior.get("artifactId") == artifact_id:
                    if artifact_update.get("append") is True:
                        combined = dict(prior)
                        combined["parts"] = list(prior.get("parts") or []) + list(
                            artifact.get("parts") or []
                        )
                        for key, value in artifact.items():
                            if key != "parts":
                                combined[key] = value
                        artifacts[index] = combined
                    else:
                        artifacts[index] = artifact
                    replaced = True
                    break
            if not replaced:
                artifacts.append(artifact)
            cached["artifacts"] = artifacts
        return cached

    @staticmethod
    def _validate_start_binding(
        task: dict[str, Any],
        *,
        harness_kind: str,
        mode_id: str,
        provider_id: str,
        model_id: str,
        workspace: dict[str, Any],
    ) -> None:
        external = task["metadata"]["externalHarness"]
        expected_identity = {
            "modeId": mode_id,
            "model": {"providerId": provider_id, "modelId": model_id},
        }
        actual_workspace = external["workspace"]
        if (
            external["harness"]["kind"] != harness_kind
            or external["identity"]["requested"] != expected_identity
            or any(
                actual_workspace.get(key) != workspace.get(key)
                for key in ("id", "strategy", "isolation")
            )
        ):
            raise ExternalHarnessError(
                "CONTEXT_BINDING_MISMATCH",
                "accepted task does not match requested harness identity or workspace",
                task_id=task["id"],
                context_id=task["contextId"],
                operation="start",
            )

    @staticmethod
    def _validate_immutable_binding(
        prior: dict[str, Any], task: dict[str, Any]
    ) -> None:
        if prior.get("contextId") != task.get("contextId"):
            raise ExternalHarnessError(
                "CONTEXT_BINDING_MISMATCH",
                "task context binding changed",
                task_id=task["id"],
                context_id=task["contextId"],
                operation="store",
            )
        prior_external = prior["metadata"]["externalHarness"]
        external = task["metadata"]["externalHarness"]
        immutable_paths = (
            ("harness", "kind"),
            ("workspace", "id"),
            ("workspace", "strategy"),
            ("workspace", "isolation"),
            ("identity", "requested"),
        )
        for section, key in immutable_paths:
            if prior_external[section].get(key) != external[section].get(key):
                raise ExternalHarnessError(
                    "CONTEXT_BINDING_MISMATCH",
                    f"task {section}.{key} binding changed",
                    task_id=task["id"],
                    context_id=task["contextId"],
                    operation="store",
                )
        for section, keys in (
            ("harness", ("adapterVersion", "runtimeVersion")),
            ("workspace", ("leaseId", "revision", "rootUri")),
            ("identity", ("effective", "proof")),
        ):
            before = prior_external[section]
            after = external[section]
            for key in keys:
                if key in before and before[key] != after.get(key):
                    raise ExternalHarnessError(
                        "CONTEXT_BINDING_MISMATCH",
                        f"immutable task {section}.{key} changed",
                        task_id=task["id"],
                        context_id=task["contextId"],
                        operation="store",
                    )
        prior_stream = prior_external["stream"]
        stream = external["stream"]
        if (
            stream["latestSequence"] < prior_stream["latestSequence"]
            or stream["oldestRetainedSequence"]
            < prior_stream["oldestRetainedSequence"]
            or external["lifecycle"]["attempt"]
            < prior_external["lifecycle"]["attempt"]
        ):
            raise ExternalHarnessError(
                "INVALID_TRANSITION",
                "task sequence or attempt counter regressed",
                task_id=task["id"],
                context_id=task["contextId"],
                operation="store",
            )

    def _validate_start(
        self,
        kind: str,
        mode: str,
        provider: str,
        model: str,
        workspace: dict[str, Any],
        prompt: str,
        key: str,
    ) -> None:
        if kind not in _HARNESS_KINDS:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "unknown external harness kind"
            )
        if not all(isinstance(value, str) and value for value in (mode, provider, model)):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "strict identity fields are required"
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "external harness prompt is required"
            )
        self._validate_idempotency_key(key, "start")
        self._validate_workspace(workspace)

    def _validate_workspace(self, workspace: dict[str, Any]) -> None:
        if not isinstance(workspace, dict) or workspace.get("isolation") != "hard":
            raise ExternalHarnessError(
                "WORKSPACE_ISOLATION_UNAVAILABLE",
                "hard workspace isolation is required",
                operation="start",
            )
        allowed = {
            "id",
            "strategy",
            "isolation",
            "rootUri",
            "baseRevision",
            "repository",
        }
        unknown = set(workspace) - allowed
        if unknown:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "workspace request has unknown fields"
            )
        workspace_id = workspace.get("id")
        if not isinstance(workspace_id, str) or not _ULID_RE.fullmatch(workspace_id):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "workspace id must be a ULID"
            )
        strategy = workspace.get("strategy")
        if strategy not in {"existing", "git-worktree", "scratch", "remote-managed"}:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "invalid workspace strategy"
            )
        if strategy in {"existing", "git-worktree"}:
            root_uri = workspace.get("rootUri")
            if not _absolute_file_uri(root_uri):
                raise ExternalHarnessError(
                    "CONTRACT_UNSUPPORTED",
                    "workspace rootUri must be an absolute file URI",
                )
        if strategy == "git-worktree" and not (
            isinstance(workspace.get("baseRevision"), str)
            and workspace["baseRevision"]
        ):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED", "git-worktree requires baseRevision"
            )
        if strategy == "remote-managed":
            repository = workspace.get("repository")
            if not isinstance(repository, dict) or set(repository) != {
                "url",
                "revision",
            }:
                raise ExternalHarnessError(
                    "CONTRACT_UNSUPPORTED",
                    "remote-managed requires repository url and revision",
                )
            url = repository.get("url")
            revision = repository.get("revision")
            if not isinstance(url, str) or not url or not isinstance(revision, str) or not revision:
                raise ExternalHarnessError(
                    "CONTRACT_UNSUPPORTED",
                    "remote-managed requires repository url and revision",
                )
            parsed = urllib.parse.urlsplit(url)
            if parsed.username or parsed.password:
                raise ExternalHarnessError(
                    "CONTRACT_UNSUPPORTED",
                    "repository URL must not embed credentials",
                )

    @staticmethod
    def _validate_idempotency_key(key: str, operation: str) -> None:
        if not isinstance(key, str) or not _IDEMPOTENCY_RE.fullmatch(key):
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "invalid idempotency key",
                operation=operation,
            )

    @staticmethod
    def _validate_task(
        task: Any,
        operation: str,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer returned invalid task",
                retryable=True,
                task_id=task_id,
                operation=operation,
            )
        if not isinstance(task.get("id"), str) or not isinstance(
            task.get("contextId"), str
        ):
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer task omitted id or context",
                retryable=True,
                task_id=task_id,
                operation=operation,
            )
        if task_id and task["id"] != task_id:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer returned a different task id",
                retryable=False,
                task_id=task_id,
                operation=operation,
            )
        state = _task_state(task)
        if state not in _ALL_STATES:
            raise ExternalHarnessError(
                "UPSTREAM_PROTOCOL_ERROR",
                "peer task has invalid lifecycle state",
                retryable=True,
                task_id=task.get("id"),
                context_id=task.get("contextId"),
                operation=operation,
            )
        _validate_task_metadata(task, state, operation)
        return task

    def _validate_stored_tenant(self, request: dict[str, Any]) -> None:
        stored_tenant = str((request.get("params") or {}).get("tenant") or "")
        if stored_tenant != self.tenant:
            raise ExternalHarnessError(
                "CONTRACT_UNSUPPORTED",
                "selected tenant changed while retrying request",
            )

    @staticmethod
    def _parse_sse(
        chunks: Iterable[str], request_id: str
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        buffer = ""
        for chunk in chunks:
            if not isinstance(chunk, str):
                raise ExternalHarnessError(
                    "STREAM_CORRUPT",
                    "stream yielded non-text data",
                    retryable=True,
                    operation="subscribe",
                )
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                parsed = _parse_sse_block(block, request_id)
                if parsed is not None:
                    yield parsed
        if buffer.strip() and not buffer.lstrip().startswith(":"):
            raise ExternalHarnessError(
                "STREAM_CORRUPT",
                "stream ended with an incomplete event",
                retryable=True,
                operation="subscribe",
            )


def _parse_sse_block(
    block: str, request_id: str
) -> tuple[str, dict[str, Any]] | None:
    event_id = ""
    data: list[str] = []
    for line in block.split("\n"):
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if not data:
        return None
    try:
        envelope = json.loads("\n".join(data))
    except json.JSONDecodeError as exc:
        raise ExternalHarnessError(
            "STREAM_CORRUPT",
            "malformed stream payload",
            retryable=True,
            operation="subscribe",
        ) from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("jsonrpc") != "2.0"
        or envelope.get("id") != request_id
        or not isinstance(envelope.get("result"), dict)
    ):
        raise ExternalHarnessError(
            "STREAM_CORRUPT",
            "invalid stream JSON-RPC envelope",
            retryable=True,
            operation="subscribe",
        )
    return event_id, envelope["result"]


def _event_stream_metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        stream = (metadata.get("externalHarness") or {}).get("stream")
        if isinstance(stream, dict):
            return stream
    for key in ("statusUpdate", "artifactUpdate"):
        update = event.get(key)
        if isinstance(update, dict):
            metadata = update.get("metadata")
            if isinstance(metadata, dict):
                stream = (metadata.get("externalHarness") or {}).get("stream")
                if isinstance(stream, dict):
                    return stream
    return {}


def _snapshot_latest_sequence(task: dict[str, Any]) -> int:
    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        return 0
    external = metadata.get("externalHarness")
    if not isinstance(external, dict):
        return 0
    stream = external.get("stream")
    if not isinstance(stream, dict):
        return 0
    value = stream.get("latestSequence")
    return value if isinstance(value, int) and value >= 0 else 0


def _absolute_file_uri(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "file"
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
    )


def _validate_rpc_url(value: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ExternalHarnessError(
            "CONTRACT_UNSUPPORTED",
            "Agent Card JSONRPC interface URL is invalid",
            operation="preflight",
        ) from exc
    if parsed.username or parsed.password or not parsed.hostname:
        raise ExternalHarnessError(
            "CONTRACT_UNSUPPORTED",
            "Agent Card JSONRPC interface URL is unsafe",
            operation="preflight",
        )
    if parsed.scheme != "https" and parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ExternalHarnessError(
            "CONTRACT_UNSUPPORTED",
            "non-loopback Agent Card interface requires TLS",
            operation="preflight",
        )


def _validate_task_metadata(
    task: dict[str, Any], state: str, operation: str
) -> None:
    metadata = task.get("metadata")
    external = metadata.get("externalHarness") if isinstance(metadata, dict) else None
    if not isinstance(external, dict) or external.get("contractVersion") != "1.0":
        raise _invalid_task_metadata(task, operation, "contract metadata")

    harness = external.get("harness")
    if not isinstance(harness, dict) or harness.get("kind") not in _HARNESS_KINDS:
        raise _invalid_task_metadata(task, operation, "harness identity")

    identity = external.get("identity")
    requested = identity.get("requested") if isinstance(identity, dict) else None
    requested_model = requested.get("model") if isinstance(requested, dict) else None
    if (
        not isinstance(identity, dict)
        or not isinstance(requested, dict)
        or not isinstance(requested.get("modeId"), str)
        or not requested["modeId"]
        or not isinstance(requested_model, dict)
        or not isinstance(requested_model.get("providerId"), str)
        or not requested_model["providerId"]
        or not isinstance(requested_model.get("modelId"), str)
        or not requested_model["modelId"]
        or not isinstance(identity.get("verificationStatus"), str)
        or not identity["verificationStatus"]
    ):
        raise _invalid_task_metadata(task, operation, "requested identity")
    if state in {"TASK_STATE_WORKING", "TASK_STATE_COMPLETED"}:
        if (
            identity.get("verificationStatus") != "verified"
            or identity.get("effective") != requested
        ):
            raise _invalid_task_metadata(task, operation, "verified effective identity")

    workspace = external.get("workspace")
    if (
        not isinstance(workspace, dict)
        or not isinstance(workspace.get("id"), str)
        or not _ULID_RE.fullmatch(workspace["id"])
        or workspace.get("strategy")
        not in {"existing", "git-worktree", "scratch", "remote-managed"}
        or workspace.get("isolation") != "hard"
    ):
        raise _invalid_task_metadata(task, operation, "workspace binding")

    lifecycle = external.get("lifecycle")
    phase = lifecycle.get("phase") if isinstance(lifecycle, dict) else None
    allowed_phases = {
        "TASK_STATE_SUBMITTED": {"queued", "identity-check"},
        "TASK_STATE_WORKING": {"starting", "running", "canceling"},
        "TASK_STATE_INPUT_REQUIRED": {"input-required"},
        "TASK_STATE_AUTH_REQUIRED": {"auth-required"},
        "TASK_STATE_COMPLETED": {"completed"},
        "TASK_STATE_FAILED": {"failed", "dead-runtime"},
        "TASK_STATE_CANCELED": {"canceled"},
        "TASK_STATE_REJECTED": {"rejected"},
    }
    if (
        not isinstance(lifecycle, dict)
        or phase not in allowed_phases[state]
        or not isinstance(lifecycle.get("attempt"), int)
        or lifecycle["attempt"] < 1
        or "reasonCode" not in lifecycle
        or not isinstance(lifecycle.get("retryable"), bool)
        or not isinstance(lifecycle.get("cancelRequested"), bool)
    ):
        raise _invalid_task_metadata(task, operation, "lifecycle")
    reason = lifecycle.get("reasonCode")
    if state in {
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED",
        "TASK_STATE_CANCELED",
    } and (not isinstance(reason, str) or not reason):
        raise _invalid_task_metadata(task, operation, "lifecycle reason")
    if state == "TASK_STATE_CANCELED" and reason != "CANCELED_BY_CALLER":
        raise _invalid_task_metadata(task, operation, "cancellation reason")

    stream = external.get("stream")
    latest = stream.get("latestSequence") if isinstance(stream, dict) else None
    oldest = stream.get("oldestRetainedSequence") if isinstance(stream, dict) else None
    if (
        not isinstance(latest, int)
        or latest < 0
        or not isinstance(oldest, int)
        or oldest < 1
        or oldest > latest + 1
    ):
        raise _invalid_task_metadata(task, operation, "stream cursor")


def _invalid_task_metadata(
    task: dict[str, Any], operation: str, field: str
) -> ExternalHarnessError:
    return ExternalHarnessError(
        "UPSTREAM_PROTOCOL_ERROR",
        f"peer task omitted required {field}",
        retryable=True,
        task_id=task.get("id"),
        context_id=task.get("contextId"),
        operation=operation,
    )


def _snapshot_transition_is_reachable(old_state: str, new_state: str) -> bool:
    if old_state == new_state:
        return True
    pending = list(_STRICT_TRANSITIONS.get(old_state, set()))
    visited: set[str] = set()
    while pending:
        state = pending.pop()
        if state == new_state:
            return True
        if state in visited:
            continue
        visited.add(state)
        pending.extend(_STRICT_TRANSITIONS.get(state, set()) - visited)
    return False


def _http_error(exc: urllib.error.HTTPError) -> ExternalHarnessError:
    if exc.code in (401, 403):
        return ExternalHarnessError(
            "UPSTREAM_AUTH_REQUIRED",
            f"peer rejected authentication (HTTP {exc.code})",
            retryable=False,
        )
    if exc.code == 429:
        return ExternalHarnessError(
            "UPSTREAM_BACKPRESSURE",
            "peer rate limited the request",
            retryable=True,
        )
    if 400 <= exc.code < 500:
        return ExternalHarnessError(
            "UPSTREAM_PROTOCOL_ERROR",
            f"peer rejected request with HTTP {exc.code}",
            retryable=False,
        )
    return ExternalHarnessError(
        "UPSTREAM_UNREACHABLE",
        f"peer returned HTTP {exc.code}",
        retryable=True,
    )


def _new_ulid() -> str:
    value = int.from_bytes(
        int(time.time() * 1000).to_bytes(6, "big") + secrets.token_bytes(10),
        "big",
    )
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sanitize(detail: str) -> str:
    text = " ".join(str(detail).replace("\\", "/").split())
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", text)
    try:
        from .security import redact_outbound

        text = redact_outbound(text)
    except Exception:
        pass
    return text.replace("/", "[redacted]")[:300]


def _task_state(task: dict[str, Any]) -> str:
    status = task.get("status")
    return str(status.get("state") or "") if isinstance(status, dict) else ""


def _is_terminal(task: dict[str, Any]) -> bool:
    return _task_state(task) in _TERMINAL_STATES


def _merge_dicts(left: Any, right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left) if isinstance(left, dict) else {}
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _snapshot_metadata_from_event(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop event-only cursor fields before folding metadata into a Task snapshot."""
    cleaned = dict(metadata)
    external = cleaned.get("externalHarness")
    if not isinstance(external, dict):
        return cleaned
    external = dict(external)
    cleaned["externalHarness"] = external
    stream = external.get("stream")
    if isinstance(stream, dict):
        stream = dict(stream)
        for key in ("sequence", "eventId", "blockType"):
            stream.pop(key, None)
        external["stream"] = stream
    return cleaned
