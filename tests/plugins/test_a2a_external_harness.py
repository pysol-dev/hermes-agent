import json
import socket
import threading
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from unittest.mock import patch

import pytest

from plugins.platforms.a2a import tools
from plugins.platforms.a2a.external_harness import (
    EXTENSION_URI,
    ExternalHarnessClient,
    ExternalHarnessError,
    StdlibA2ATransport,
)


WORKSPACE_ID = "01J00000000000000000000000"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.cards = [self.card(), self.card(extended=True)]
        self.responses = []
        self.streams = []

    @staticmethod
    def card(extended=False):
        return {
            "supportedInterfaces": [
                {
                    "url": "https://peer/rpc",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                    "tenant": "t",
                }
            ],
            "capabilities": {"streaming": True, "extendedAgentCard": True},
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer"}
            },
            "security": [{"bearer": []}],
            "extensions": [
                {
                    "uri": EXTENSION_URI,
                    "required": False,
                    "params": {
                        "contractVersion": "1.0",
                        "capabilitiesMethod": "externalHarness.capabilities.get",
                        "metadataField": "metadata.externalHarness",
                        "eventCursorHeader": "Last-Event-ID",
                    },
                }
            ],
        }

    def get_json(self, url, headers):
        self.calls.append(("get", url, headers))
        return self.cards.pop(0)

    def rpc(self, url, request, headers):
        self.calls.append(("rpc", request["method"], request, headers))
        if request["method"] == "externalHarness.capabilities.get":
            return {
                "result": {
                    "contractVersion": "1.0",
                    "protocolVersion": "1.0",
                    "adapter": {
                        "name": "test-adapter",
                        "version": "1.0.0",
                        "instanceId": "01J00000000000000000000001",
                    },
                    "harnesses": [
                        {
                            "kind": "opencode-omo",
                            "runtimeVersion": "1.0.0",
                            "modes": [{"id": "hephaestus"}],
                            "models": [
                                {
                                    "providerId": "openai",
                                    "modelId": "gpt",
                                    "available": True,
                                }
                            ],
                            "features": {
                                key: True for key in ExternalHarnessClient.REQUIRED_FEATURES
                            },
                        }
                    ],
                    "workspace": {
                        "strategies": [
                            "existing",
                            "git-worktree",
                            "scratch",
                            "remote-managed",
                        ],
                        "isolation": "hard",
                    },
                    "limits": {
                        "maxConcurrentTasks": 8,
                        "maxMessageBytes": 1_048_576,
                        "cancelConfirmTimeoutMs": 10_000,
                        "taskRetentionSeconds": 2_592_000,
                        "eventRetentionSeconds": 604_800,
                    },
                }
            }
        return self.responses.pop(0)

    def sse(self, url, request, headers):
        self.calls.append(("sse", request["method"], request, headers))
        request_id = request["id"]
        return iter(
            chunk.replace('"id": "rpc-stream"', f'"id": "{request_id}"')
            for chunk in self.streams.pop(0)
        )


class FakeResponse:
    def __init__(self, payload, headers):
        self.payload = payload
        self.headers = headers

    def read(self):
        return self.payload

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def task(
    task_id="task-1",
    context_id="ctx-1",
    state="TASK_STATE_SUBMITTED",
    latest_sequence=0,
):
    phases = {
        "TASK_STATE_SUBMITTED": "queued",
        "TASK_STATE_WORKING": "running",
        "TASK_STATE_INPUT_REQUIRED": "input-required",
        "TASK_STATE_AUTH_REQUIRED": "auth-required",
        "TASK_STATE_COMPLETED": "completed",
        "TASK_STATE_FAILED": "failed",
        "TASK_STATE_CANCELED": "canceled",
        "TASK_STATE_REJECTED": "rejected",
    }
    reason_codes = {
        "TASK_STATE_INPUT_REQUIRED": "INPUT_REQUIRED",
        "TASK_STATE_AUTH_REQUIRED": "AUTH_REQUIRED",
        "TASK_STATE_FAILED": "RUNTIME_LOST",
        "TASK_STATE_CANCELED": "CANCELED_BY_CALLER",
        "TASK_STATE_REJECTED": "MODE_UNAVAILABLE",
    }
    requested = {
        "modeId": "hephaestus",
        "model": {"providerId": "openai", "modelId": "gpt"},
    }
    identity = {
        "requested": requested,
        "verificationStatus": (
            "verified"
            if state not in {"TASK_STATE_SUBMITTED", "TASK_STATE_REJECTED"}
            else "pending"
        ),
    }
    if identity["verificationStatus"] == "verified":
        identity["effective"] = requested
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": state},
        "metadata": {
            "externalHarness": {
                "contractVersion": "1.0",
                "harness": {"kind": "opencode-omo"},
                "identity": identity,
                "workspace": {
                    "id": WORKSPACE_ID,
                    "strategy": "scratch",
                    "isolation": "hard",
                },
                "lifecycle": {
                    "phase": phases[state],
                    "attempt": 1,
                    "reasonCode": reason_codes.get(state),
                    "retryable": state == "TASK_STATE_FAILED",
                    "cancelRequested": False,
                },
                "stream": {
                    "latestSequence": latest_sequence,
                    "oldestRetainedSequence": 1,
                },
            }
        },
    }


def stream_event(task_id, sequence, *, state=None):
    state = state or "TASK_STATE_WORKING"
    snapshot = task(task_id=task_id, state=state)
    external = snapshot["metadata"]["externalHarness"]
    return {
        "statusUpdate": {
            "taskId": task_id,
            "contextId": "ctx-1",
            "status": {"state": state},
            "metadata": {
                "externalHarness": {
                    "identity": external["identity"],
                    "lifecycle": external["lifecycle"],
                    "stream": {
                        "sequence": sequence,
                        "eventId": f"{task_id}:{sequence}",
                        "latestSequence": sequence,
                        "oldestRetainedSequence": 1,
                    }
                }
            },
        }
    }


def sse_frame(request_id, event):
    stream = event["statusUpdate"]["metadata"]["externalHarness"]["stream"]
    payload = {"jsonrpc": "2.0", "id": request_id, "result": event}
    return f"id: {stream['eventId']}\ndata: {json.dumps(payload)}\n\n"


def sse_snapshot(request_id, snapshot):
    payload = {"jsonrpc": "2.0", "id": request_id, "result": {"task": snapshot}}
    return f"data: {json.dumps(payload)}\n\n"


def client(tmp_path, transport, token="test-token"):
    return ExternalHarnessClient(
        "https://peer", transport, tmp_path / "runs.sqlite", token=token
    )


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_request():
    return {
        "harness_kind": "opencode-omo",
        "mode_id": "hephaestus",
        "provider_id": "openai",
        "model_id": "gpt",
        "workspace": {
            "id": WORKSPACE_ID,
            "strategy": "scratch",
            "isolation": "hard",
        },
        "prompt": "do work",
        "idempotency_key": "request-key-0001",
    }


def test_start_is_preflighted_durable_and_idempotent(tmp_path):
    transport = FakeTransport()
    transport.responses = [{"result": {"task": task()}}, {"result": task()}]
    harness = client(tmp_path, transport)
    request = start_request()

    first = harness.start(**request)
    second = harness.start(**request)

    assert first == second == task()
    assert [call[1] for call in transport.calls if call[0] == "rpc"] == [
        "externalHarness.capabilities.get",
        "SendMessage",
    ]
    sent = [
        call
        for call in transport.calls
        if call[0] == "rpc" and call[1] == "SendMessage"
    ][0][2]
    metadata = sent["params"]["metadata"]["externalHarness"]
    assert metadata["identity"] == {
        "modeId": "hephaestus",
        "model": {"providerId": "openai", "modelId": "gpt"},
    }
    assert metadata["workspace"] == request["workspace"]
    assert harness.cached_task("task-1") == task()


def test_rejects_nonconforming_cards_without_sending_work(tmp_path):
    transport = FakeTransport()
    transport.cards[0]["supportedInterfaces"][0]["protocolVersion"] = "0.3"

    with pytest.raises(ExternalHarnessError, match="CONTRACT_UNSUPPORTED") as exc:
        client(tmp_path, transport).start(**start_request())

    assert exc.value.reason_code == "CONTRACT_UNSUPPORTED"
    assert not [call for call in transport.calls if call[0] == "rpc"]


def test_follow_up_cancel_list_and_sse_replay_are_durable(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task())
    transport.responses = [
        {"result": {"task": task()}},
        {"result": {"tasks": [task()]}},
        {"result": task(state="TASK_STATE_CANCELED", latest_sequence=2)},
    ]
    transport.streams = [
        [
            sse_snapshot("rpc-stream", task()),
            sse_frame("rpc-stream", stream_event("task-1", 1)),
            sse_frame("rpc-stream", stream_event("task-1", 1)),
            sse_frame("rpc-stream", stream_event("task-1", 2)),
        ]
    ]

    assert harness.follow_up(
        "task-1", "ctx-1", "answer", "request-key-0002", interaction_id="i-1"
    ) == task()
    assert harness.list() == [task()]
    events = list(harness.subscribe("task-1"))
    assert [
        event["statusUpdate"]["metadata"]["externalHarness"]["stream"][
            "sequence"
        ]
        for event in events
    ] == [1, 2]
    assert harness.cancel("task-1") == task(
        state="TASK_STATE_CANCELED", latest_sequence=2
    )
    assert any(
        call[0] == "sse" and call[3].get("Last-Event-ID") is None
        for call in transport.calls
    )


def test_stream_gap_is_typed_and_sanitized(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task())
    transport.streams = [[
        sse_snapshot("rpc-stream", task()),
        sse_frame("rpc-stream", stream_event("task-1", 2)),
    ]]

    with pytest.raises(ExternalHarnessError) as exc:
        list(harness.subscribe("task-1", reconnect_attempts=0))

    assert exc.value.reason_code == "STREAM_CORRUPT"
    assert "/" not in exc.value.detail


def test_get_always_refreshes_remote_snapshot(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task(state="TASK_STATE_SUBMITTED"))
    harness.rpc_url = "https://peer/rpc"
    harness.tenant = "t"
    transport.responses = [{"result": task(state="TASK_STATE_COMPLETED")}]

    assert harness.get("task-1")["status"]["state"] == "TASK_STATE_COMPLETED"


def test_start_workspace_strategy_is_validated_before_network(tmp_path):
    transport = FakeTransport()
    request = start_request()
    request["workspace"] = {
        "id": WORKSPACE_ID,
        "strategy": "existing",
        "isolation": "hard",
    }
    with pytest.raises(ExternalHarnessError, match="rootUri"):
        client(tmp_path, transport).start(**request)
    assert transport.calls == []


def test_reopened_client_recovers_idempotent_start_without_remote_side_effect(tmp_path):
    transport = FakeTransport()
    transport.responses = [{"result": {"task": task()}}]
    request = start_request()
    assert client(tmp_path, transport).start(**request) == task()
    before = len(transport.calls)
    assert client(tmp_path, transport).start(**request) == task()
    assert len(transport.calls) == before


def test_cancel_is_idempotently_exposed_as_authoritative_task(tmp_path):
    transport = FakeTransport()
    transport.responses = [
        {"result": task(state="TASK_STATE_CANCELED")},
        {"result": task(state="TASK_STATE_CANCELED")},
    ]
    harness = client(tmp_path, transport)
    harness.rpc_url = "https://peer/rpc"
    assert harness.cancel("task-1") == task(state="TASK_STATE_CANCELED")
    assert harness.cancel("task-1") == task(state="TASK_STATE_CANCELED")


def test_stdlib_transport_maps_auth_and_validates_headers():
    error = urllib.error.HTTPError("https://peer", 401, "no", Message(), BytesIO())
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(ExternalHarnessError) as exc:
            StdlibA2ATransport().get_json(
                "https://peer",
                {"A2A-Version": "1.0", "A2A-Extensions": EXTENSION_URI},
            )
    assert exc.value.reason_code == "UPSTREAM_AUTH_REQUIRED"


def test_external_harness_tools_register_and_format_failures(monkeypatch):
    calls = []

    class Context:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    tools.register_tools(Context())
    names = {item["name"] for item in calls}
    assert {
        "a2a_external_harness_start",
        "a2a_external_harness_status",
        "a2a_external_harness_list",
        "a2a_external_harness_read_or_subscribe",
        "a2a_external_harness_follow_up",
        "a2a_external_harness_cancel",
    } <= names
    monkeypatch.setattr(
        tools,
        "_external_harness_client",
        lambda _args: (_ for _ in ()).throw(
            ExternalHarnessError("UPSTREAM_AUTH_REQUIRED", "denied")
        ),
    )
    assert "EXTERNAL HARNESS ERROR [UPSTREAM_AUTH_REQUIRED]" in (
        tools.a2a_external_harness_status({"peer": "peer", "task_id": "task"})
    )
    assert "peer=peer" in tools.a2a_external_harness_status(
        {"peer": "peer", "task_id": "task"}
    )


def test_failed_task_format_is_loud_and_includes_safe_detail():
    failed = task(state="TASK_STATE_FAILED")
    failed["status"]["message"] = {
        "role": "ROLE_AGENT",
        "parts": [{"text": "runtime exited", "mediaType": "text/plain"}],
    }

    rendered = tools._format_external_task(failed)
    assert "task-1" in rendered and "ctx-1" in rendered
    assert "requested=hephaestus@openai/gpt" in rendered
    assert f"Workspace: {WORKSPACE_ID} | scratch | isolation=hard" in rendered
    assert "LOUD STATUS: reasonCode=RUNTIME_LOST retryable=True" in rendered
    assert "runtime exited" in rendered


def test_start_persists_intent_before_remote_side_effect_and_reuses_message_id(
    tmp_path, monkeypatch
):
    transport = FakeTransport()
    request = start_request()
    original_rpc = transport.rpc
    sent_ids = []

    def fail_once(url, rpc_request, headers):
        if rpc_request["method"] == "SendMessage":
            sent_ids.append(rpc_request["params"]["message"]["messageId"])
            if len(sent_ids) == 1:
                raise ExternalHarnessError(
                    "UPSTREAM_UNREACHABLE", "disconnected", retryable=True
                )
            return {"result": {"task": task()}}
        return original_rpc(url, rpc_request, headers)

    monkeypatch.setattr(transport, "rpc", fail_once)
    first = client(tmp_path, transport)
    with pytest.raises(ExternalHarnessError, match="UPSTREAM_UNREACHABLE"):
        first.start(**request)
    row = first.db.execute(
        "SELECT response_json, request_json FROM requests WHERE idempotency_key = ?",
        (request["idempotency_key"],),
    ).fetchone()
    assert row is not None and not row["response_json"] and row["request_json"]

    transport.cards = [transport.card(), transport.card(extended=True)]
    recovered = client(tmp_path, transport)
    assert recovered.start(**request) == task()
    assert len(sent_ids) == 2
    assert sent_ids[0] == sent_ids[1]


def test_restart_status_rediscovers_interface_and_refreshes_remote(tmp_path):
    first_transport = FakeTransport()
    first = client(tmp_path, first_transport)
    first._store_task(task())
    first.close()

    transport = FakeTransport()
    transport.responses = [{"result": task(state="TASK_STATE_WORKING")}]
    restarted = client(tmp_path, transport)
    assert restarted.get("task-1")["status"]["state"] == "TASK_STATE_WORKING"
    assert [call[0] for call in transport.calls[:2]] == ["get", "get"]
    assert transport.calls[-1][1] == "GetTask"


def test_terminal_snapshot_is_immutable(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task(state="TASK_STATE_COMPLETED"))
    with pytest.raises(ExternalHarnessError, match="INVALID_TRANSITION"):
        harness._store_task(task(state="TASK_STATE_FAILED"))


def test_mode_and_model_rejections_keep_contract_reason_codes(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    with pytest.raises(ExternalHarnessError) as mode_error:
        harness._preflight("opencode-omo", "missing", "openai", "gpt")
    assert mode_error.value.reason_code == "MODE_UNAVAILABLE"

    transport = FakeTransport()
    harness = client(tmp_path, transport)
    with pytest.raises(ExternalHarnessError) as model_error:
        harness._preflight("opencode-omo", "hephaestus", "openai", "missing")
    assert model_error.value.reason_code == "MODEL_UNAVAILABLE"


def test_workspace_and_idempotency_identifiers_are_strict(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    request = start_request()
    request["workspace"]["id"] = "not-a-ulid"
    with pytest.raises(ExternalHarnessError, match="workspace id"):
        harness.start(**request)
    request = start_request()
    request["idempotency_key"] = "contains spaces 0001"
    with pytest.raises(ExternalHarnessError, match="idempotency"):
        harness.start(**request)
    assert transport.calls == []


def test_sse_snapshot_sets_baseline_then_resume_uses_last_event_id(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task())
    snapshot = task()
    snapshot["metadata"]["externalHarness"]["stream"] = {
        "latestSequence": 4,
        "oldestRetainedSequence": 1,
    }
    transport.streams = [
        [
            "data: "
            + json.dumps(
                {"jsonrpc": "2.0", "id": "rpc-stream", "result": {"task": snapshot}}
            )
            + "\n\n",
            sse_frame("rpc-stream", stream_event("task-1", 5)),
        ],
        [sse_frame("rpc-stream", stream_event("task-1", 6))],
    ]

    first = list(harness.subscribe("task-1"))
    second = list(harness.subscribe("task-1"))
    assert [
        event["statusUpdate"]["metadata"]["externalHarness"]["stream"][
            "sequence"
        ]
        for event in first
    ] == [5]
    assert [
        event["statusUpdate"]["metadata"]["externalHarness"]["stream"][
            "sequence"
        ]
        for event in second
    ] == [6]
    subscribe_calls = [call for call in transport.calls if call[0] == "sse"]
    assert subscribe_calls[0][3].get("Last-Event-ID") is None
    assert subscribe_calls[1][3]["Last-Event-ID"] == "task-1:5"


def test_stream_disconnect_gets_status_and_reconnects_from_cursor(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task())

    def broken_stream():
        yield sse_snapshot("rpc-stream", task())
        yield sse_frame("rpc-stream", stream_event("task-1", 1))
        raise ExternalHarnessError(
            "UPSTREAM_UNREACHABLE", "connection lost", retryable=True
        )

    transport.streams = [
        broken_stream(),
        [sse_frame("rpc-stream", stream_event("task-1", 2))],
    ]
    transport.responses = [
        {"result": task(state="TASK_STATE_WORKING", latest_sequence=1)}
    ]

    events = list(harness.subscribe("task-1"))
    assert [
        event["statusUpdate"]["metadata"]["externalHarness"]["stream"][
            "sequence"
        ]
        for event in events
    ] == [1, 2]
    subscribe_calls = [call for call in transport.calls if call[0] == "sse"]
    assert subscribe_calls[1][3]["Last-Event-ID"] == "task-1:1"
    assert any(call[0] == "rpc" and call[1] == "GetTask" for call in transport.calls)


def test_completion_wins_cancel_race(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    completed = task(state="TASK_STATE_COMPLETED")
    harness._store_task(completed)
    transport.responses = [{"result": task(state="TASK_STATE_CANCELED")}]

    with pytest.raises(ExternalHarnessError, match="INVALID_TRANSITION"):
        harness.cancel("task-1")
    assert harness.cached_task("task-1") == completed


def test_strict_client_requires_bearer_authentication(tmp_path):
    with pytest.raises(ExternalHarnessError) as exc:
        ExternalHarnessClient(
            "https://peer", FakeTransport(), tmp_path / "runs.sqlite"
        )
    assert exc.value.reason_code == "UPSTREAM_AUTH_REQUIRED"


def test_strict_client_rejects_credentials_embedded_in_peer_url(tmp_path):
    with pytest.raises(ExternalHarnessError, match="must not embed credentials"):
        ExternalHarnessClient(
            "https://user:secret@peer",
            FakeTransport(),
            tmp_path / "runs.sqlite",
            token="separate-token",
        )


def test_stdlib_transport_allows_public_card_without_extension_echo():
    payload = json.dumps(FakeTransport.card()).encode()
    response = FakeResponse(payload, {"A2A-Version": "1.0"})
    with patch("urllib.request.urlopen", return_value=response):
        card = StdlibA2ATransport().get_json(
            "https://peer/.well-known/agent-card.json", {"A2A-Version": "1.0"}
        )
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"


def test_stdlib_transport_rejects_mismatched_jsonrpc_id():
    request = {
        "jsonrpc": "2.0",
        "id": "expected",
        "method": "GetTask",
        "params": {"id": "t"},
    }
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "wrong", "result": task()}
    ).encode()
    response = FakeResponse(
        payload,
        {"A2A-Version": "1.0", "A2A-Extensions": EXTENSION_URI},
    )
    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(ExternalHarnessError, match="correlation"):
            StdlibA2ATransport().rpc(
                "https://peer",
                request,
                {"A2A-Version": "1.0", "A2A-Extensions": EXTENSION_URI},
            )


def test_task_snapshots_require_full_external_harness_metadata(tmp_path):
    transport = FakeTransport()
    transport.responses = [
        {
            "result": {
                "id": "task-1",
                "contextId": "ctx-1",
                "status": {"state": "TASK_STATE_WORKING"},
            }
        }
    ]
    harness = client(tmp_path, transport)
    harness.rpc_url = "https://peer/rpc"

    with pytest.raises(ExternalHarnessError) as exc:
        harness.get("task-1")

    assert exc.value.reason_code == "UPSTREAM_PROTOCOL_ERROR"
    assert "metadata" in exc.value.detail


def test_start_rejects_changed_identity_binding(tmp_path):
    transport = FakeTransport()
    mismatched = task()
    mismatched["metadata"]["externalHarness"]["identity"]["requested"][
        "modeId"
    ] = "sisyphus"
    transport.responses = [{"result": {"task": mismatched}}]

    with pytest.raises(ExternalHarnessError) as exc:
        client(tmp_path, transport).start(**start_request())

    assert exc.value.reason_code == "CONTEXT_BINDING_MISMATCH"


def test_list_follows_opaque_pagination_tokens(tmp_path):
    transport = FakeTransport()
    transport.responses = [
        {"result": {"tasks": [task()], "nextPageToken": "page-2"}},
        {"result": {"tasks": [task(task_id="task-2")]}},
    ]

    tasks = client(tmp_path, transport).list(page_size=1)

    assert [item["id"] for item in tasks] == ["task-1", "task-2"]
    list_calls = [
        call for call in transport.calls if call[0] == "rpc" and call[1] == "ListTasks"
    ]
    assert list_calls[1][2]["params"]["pageToken"] == "page-2"


def test_cancel_rejects_false_nonterminal_success(tmp_path):
    transport = FakeTransport()
    transport.responses = [{"result": task(state="TASK_STATE_WORKING")}]
    harness = client(tmp_path, transport)
    harness.rpc_url = "https://peer/rpc"
    harness._store_task(task())

    with pytest.raises(ExternalHarnessError) as exc:
        harness.cancel("task-1")

    assert exc.value.reason_code == "UPSTREAM_PROTOCOL_ERROR"
    assert "nonterminal" in exc.value.detail
    assert harness.cached_task("task-1") == task()


def test_stream_binding_error_rolls_back_event_and_cursor(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task())
    event = stream_event("task-1", 1)
    event["statusUpdate"]["contextId"] = "ctx-other"
    transport.streams = [[
        sse_snapshot("rpc-stream", task()),
        sse_frame("rpc-stream", event),
    ]]

    with pytest.raises(ExternalHarnessError) as exc:
        list(harness.subscribe("task-1", reconnect_attempts=0))

    assert exc.value.reason_code == "STREAM_CORRUPT"
    row = harness.db.execute(
        "SELECT cursor FROM runs WHERE peer_url=? AND task_id=?",
        (harness.storage_scope, "task-1"),
    ).fetchone()
    assert row["cursor"] == 0
    assert harness.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_stream_rejects_events_after_terminal_without_mutating_terminal(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    harness._store_task(task())
    transport.streams = [[
        sse_snapshot("rpc-stream", task()),
        sse_frame("rpc-stream", stream_event("task-1", 1)),
        sse_frame(
            "rpc-stream",
            stream_event("task-1", 2, state="TASK_STATE_COMPLETED"),
        ),
        sse_frame("rpc-stream", stream_event("task-1", 3)),
    ]]

    with pytest.raises(ExternalHarnessError) as exc:
        list(harness.subscribe("task-1", reconnect_attempts=0))

    assert exc.value.reason_code == "STREAM_CORRUPT"
    terminal = harness.cached_task("task-1")
    assert terminal["status"]["state"] == "TASK_STATE_COMPLETED"
    assert terminal["metadata"]["externalHarness"]["stream"]["latestSequence"] == 2
    assert harness.db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_expired_cursor_refreshes_status_and_surfaces_incomplete_replay(tmp_path):
    transport = FakeTransport()
    harness = client(tmp_path, transport)
    running = task(state="TASK_STATE_WORKING", latest_sequence=1)
    harness._store_task(running)
    harness._set_cursor("task-1", 1)

    def expired_stream():
        raise ExternalHarnessError(
            "EVENT_CURSOR_EXPIRED", "expired", retryable=False
        )
        yield ""  # pragma: no cover - make this an iterator

    transport.streams = [expired_stream()]
    transport.responses = [{"result": running}]

    with pytest.raises(ExternalHarnessError) as exc:
        list(harness.subscribe("task-1"))

    assert exc.value.reason_code == "EVENT_CURSOR_EXPIRED"
    assert "reconstruction is unavailable" in exc.value.detail
    assert any(
        call[0] == "rpc" and call[1] == "GetTask" for call in transport.calls
    )


LEGAL_TRANSITIONS = {
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


@pytest.mark.parametrize(
    ("old_state", "new_state"),
    [
        (old_state, new_state)
        for old_state, next_states in LEGAL_TRANSITIONS.items()
        for new_state in sorted(next_states)
    ],
)
def test_event_ledger_accepts_every_legal_lifecycle_edge(
    tmp_path, old_state, new_state
):
    harness = client(tmp_path, FakeTransport())
    harness._store_task(task(state=old_state))

    harness._store_task(task(state=new_state), strict_transition=True)

    assert harness.cached_task("task-1")["status"]["state"] == new_state


@pytest.mark.parametrize(
    ("old_state", "new_state"),
    [
        (old_state, new_state)
        for old_state, next_states in LEGAL_TRANSITIONS.items()
        for new_state in sorted(
            {
                "TASK_STATE_SUBMITTED",
                "TASK_STATE_WORKING",
                "TASK_STATE_INPUT_REQUIRED",
                "TASK_STATE_AUTH_REQUIRED",
                "TASK_STATE_COMPLETED",
                "TASK_STATE_FAILED",
                "TASK_STATE_CANCELED",
                "TASK_STATE_REJECTED",
            }
            - next_states
            - {old_state}
        )
    ],
)
def test_event_ledger_rejects_every_illegal_lifecycle_edge(
    tmp_path, old_state, new_state
):
    harness = client(tmp_path, FakeTransport())
    harness._store_task(task(state=old_state))

    with pytest.raises(ExternalHarnessError, match="INVALID_TRANSITION"):
        harness._store_task(task(state=new_state), strict_transition=True)


def test_card_allows_additive_extension_params_but_requires_declared_bearer(tmp_path):
    transport = FakeTransport()
    transport.cards[0]["extensions"][0]["params"]["futureField"] = "ignored"
    transport.cards[1]["extensions"][0]["params"]["futureField"] = "ignored"
    transport.responses = [{"result": {"task": task()}}]
    assert client(tmp_path, transport).start(**start_request()) == task()

    transport = FakeTransport()
    transport.cards[0]["security"] = [{"unrelated": []}]
    request = start_request()
    request["idempotency_key"] = "request-key-0009"
    with pytest.raises(ExternalHarnessError, match="does not declare"):
        client(tmp_path, transport).start(**request)


def test_local_idempotency_cache_is_scoped_to_authenticated_principal(tmp_path):
    first_transport = FakeTransport()
    first_transport.responses = [{"result": {"task": task()}}]
    assert client(tmp_path, first_transport, token="principal-one").start(
        **start_request()
    ) == task()

    second_transport = FakeTransport()
    second_task = task(task_id="task-2", context_id="ctx-2")
    second_transport.responses = [{"result": {"task": second_task}}]
    assert client(tmp_path, second_transport, token="principal-two").start(
        **start_request()
    ) == second_task
    assert any(
        call[0] == "rpc" and call[1] == "SendMessage"
        for call in second_transport.calls
    )


@pytest.mark.integration
def test_live_strict_http_start_status_and_stream_round_trip(tmp_path):
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return None

        def _send_headers(self, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("A2A-Version", "1.0")
            if self.headers.get("A2A-Extensions"):
                self.send_header("A2A-Extensions", EXTENSION_URI)
            self.end_headers()

        def do_GET(self):
            assert self.headers.get("Authorization") == "Bearer test-token"
            card = FakeTransport.card()
            card["supportedInterfaces"][0]["url"] = base_url + "/rpc"
            self._send_headers()
            self.wfile.write(json.dumps(card).encode())

        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer test-token"
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            method = request["method"]
            if method == "externalHarness.capabilities.get":
                result = FakeTransport().rpc("", request, {})["result"]
            elif method == "SendMessage":
                result = {"task": task()}
            elif method == "GetTask":
                result = task(state="TASK_STATE_COMPLETED", latest_sequence=2)
            elif method == "SubscribeToTask":
                self._send_headers("text/event-stream")
                self.wfile.write(sse_snapshot(request["id"], task()).encode())
                self.wfile.write(
                    sse_frame(
                        request["id"],
                        stream_event("task-1", 1),
                    ).encode()
                )
                self.wfile.write(
                    sse_frame(
                        request["id"],
                        stream_event("task-1", 2, state="TASK_STATE_COMPLETED"),
                    ).encode()
                )
                return
            else:
                raise AssertionError(f"unexpected method: {method}")
            self._send_headers()
            self.wfile.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": request["id"], "result": result}
                ).encode()
            )

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExternalHarnessClient(
            base_url,
            StdlibA2ATransport(timeout=5),
            tmp_path / "live.sqlite",
            token="test-token",
        ) as harness:
            assert harness.start(**start_request())["status"]["state"] == (
                "TASK_STATE_SUBMITTED"
            )
            events = list(harness.subscribe("task-1", reconnect_attempts=0))
            assert events[0]["statusUpdate"]["status"]["state"] == (
                "TASK_STATE_WORKING"
            )
            assert events[-1]["statusUpdate"]["status"]["state"] == (
                "TASK_STATE_COMPLETED"
            )
            assert harness.get("task-1")["status"]["state"] == (
                "TASK_STATE_COMPLETED"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
