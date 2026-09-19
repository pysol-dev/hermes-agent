# A2A External-Harness Integration Contract (v1)

**Status:** normative implementation contract for the ACPX replacement gate

**Contract version:** `1.0`

**A2A protocol version:** `1.0` (JSON-RPC binding)

**Extension URI:** `urn:pysol-dev:a2a:extension:external-harness:v1`

**Audience:** Hermes, the `pysol-dev/opencode-a2a` fork, Codex and Claude
adapter authors, remote-agent operators, and acceptance-test authors

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, and **MAY** are
normative.

## 1. Decision and boundary

Hermes uses two deliberately separate orchestration planes:

1. **Hermes internal plane — unchanged.** `delegate_task`, Hermes child agents,
   Bot Mode, peer/profile conversations, and Kanban remain upstream-maintained
   Hermes facilities. Their IDs, persistence, workspaces, tools, and lifecycle
   do not pass through A2A and are not changed by this contract.
2. **External-harness plane — A2A only.** OpenCode with Oh My OpenAgent (OMO),
   Codex, Claude, and remote non-Hermes agents are invoked through A2A 1.0 plus
   the extension in this document.
3. **ACPX — migration source only.** No new external task may be routed through
   both A2A and ACPX. ACPX is disabled only after every gate in section 20
   passes. The rollback mechanism is a code/config snapshot, not simultaneous
   dual routing.

An ordinary A2A peer that does not implement this extension can still be used by
Hermes's generic A2A client. It is **not** eligible for the strict
external-harness path and cannot count toward ACPX replacement acceptance.

## 2. Reviewed baseline and gaps

This contract was derived from:

- A2A specification `1.0.0` and its canonical `lf.a2a.v1` ProtoJSON shapes.
- Hermes `c701ce0b2b577693f7d5b8022ba730e8c3f02f77`, especially
  `plugins/platforms/a2a/`.
- ACPX `14df4f8d96afa9958dea22d4aabad53127481939`, especially its public runtime
  contract, persisted handle state, lifecycle reconciliation, model state, and
  mode preference handling.
- `Intelligent-Internet/opencode-a2a`
  `fbea78fef2c6d0d6ddaa21d9b7695cbb2bbe1600`, the upstream source intended for
  the `pysol-dev` fork. At review time, no `pysol-dev/opencode-a2a` repository
  existed, so this exact upstream commit is the fork baseline.
- The attached architecture report and live failure evidence: an ACPX queue
  owner remained “running,” cancellation returned “nothing to cancel,” and no
  final result survived the runtime failure.

The reviewed implementations are useful baselines but do **not** satisfy this
contract yet:

| Surface | Observed gap that this contract closes |
| --- | --- |
| Hermes outbound A2A | Synchronous `a2a_call`; best-effort discovery suppresses card failures; no durable start/get/subscribe/follow-up/cancel client; no strict mode/model/workspace proof. |
| Hermes inbound A2A | In-memory task state; cancellation marks a task canceled without aborting the live turn; persistence failures are sometimes swallowed; no replayable event ledger. |
| Upstream OpenCode adapter | Durable task/session rows exist, but running execution maps are process-memory state; terminal subscribe correctly follows A2A and is unsupported; cancellation publishes canceled before proving abort; model metadata can silently fall back; path validation is not hard cross-task filesystem isolation; no OMO persona/mode proof. |
| ACPX | Supplies handles, mode/model replay, events, status, and cancellation, but its queue-owner/process status can contradict the real runtime and is the component being replaced. |

## 3. Protocol profile

### 3.1 Mandatory binding

Every conforming external-harness adapter MUST expose:

- A2A Agent Card at `GET /.well-known/agent-card.json`.
- Authenticated extended Agent Card at `GET /extendedAgentCard`.
- A2A 1.0 JSON-RPC at the selected `supportedInterfaces[].url`.
- SSE for `SendStreamingMessage` and `SubscribeToTask`.
- Header `A2A-Version: 1.0` on requests and responses.
- Header
  `A2A-Extensions: urn:pysol-dev:a2a:extension:external-harness:v1` when the
  extension is activated. The server MUST echo the activated URI.

HTTP+JSON and gRPC MAY also be offered, but JSON-RPC is the conformance and
cutover binding. A client MUST NOT silently select a pre-1.0 interface.
Production non-loopback endpoints MUST use TLS.

### 3.2 Mandatory core methods

The selected interface MUST implement these A2A 1.0 methods:

- `SendMessage`
- `SendStreamingMessage`
- `GetTask`
- `ListTasks`
- `CancelTask`
- `SubscribeToTask`
- `GetExtendedAgentCard`

Push-notification methods are optional for v1 of this contract. If advertised,
they must follow A2A 1.0 and must not replace durable polling or subscription.

### 3.3 Mandatory extension method

The extension adds one JSON-RPC method:

`externalHarness.capabilities.get`

Request params:

```json
{
  "harnessKind": "opencode-omo"
}
```

`harnessKind` is optional. If omitted, all descriptors visible to the
authenticated principal are returned. The method has no side effects.

Result:

```json
{
  "contractVersion": "1.0",
  "protocolVersion": "1.0",
  "adapter": {
    "name": "opencode-a2a",
    "version": "1.2.3",
    "instanceId": "01J..."
  },
  "harnesses": [
    {
      "kind": "opencode-omo",
      "runtimeVersion": "1.0.0",
      "modes": [
        {"id": "hephaestus", "label": "Hephaestus"}
      ],
      "models": [
        {
          "providerId": "openai",
          "modelId": "gpt-5.6-codex",
          "label": "GPT-5.6 Codex",
          "available": true
        }
      ],
      "features": {
        "durableTasks": true,
        "returnImmediately": true,
        "streaming": true,
        "replayableEvents": true,
        "followUp": true,
        "cancel": true,
        "restartRecovery": true,
        "strictIdentity": true,
        "runtimeIdentityProof": true,
        "hardWorkspaceIsolation": true
      }
    }
  ],
  "workspace": {
    "strategies": ["existing", "git-worktree", "scratch", "remote-managed"],
    "isolation": "hard"
  },
  "limits": {
    "maxConcurrentTasks": 8,
    "maxMessageBytes": 1048576,
    "cancelConfirmTimeoutMs": 10000,
    "taskRetentionSeconds": 2592000,
    "eventRetentionSeconds": 604800
  }
}
```

Every shown field is REQUIRED except `modes[].label` and `models[].label`.
Arrays may be empty when a harness is temporarily unavailable, but the adapter
must then reject starts for that harness. Mode and model IDs are case-sensitive
opaque identifiers. Clients must send exact advertised values; aliases are a UI
concern and never cross the wire. Additional fields are allowed for additive
discovery, but clients must not infer a missing required field from a default.

The public and extended Agent Cards advertise this exact extension entry (the
extended card may add deployment details but may not change these values):

```json
{
  "uri": "urn:pysol-dev:a2a:extension:external-harness:v1",
  "description": "Durable, identity-bound external harness execution",
  "required": false,
  "params": {
    "contractVersion": "1.0",
    "capabilitiesMethod": "externalHarness.capabilities.get",
    "metadataField": "metadata.externalHarness",
    "eventCursorHeader": "Last-Event-ID"
  }
}
```

`required` is false so a generic A2A client can still use the endpoint without
this profile. Hermes's strict external-harness route nevertheless requires and
activates it. Unknown extension fields are ignored; a changed value in one of
the four declared `params` fields requires a new extension URI.

## 4. Discovery and preflight

Before starting a new context, Hermes MUST perform this sequence:

1. Fetch the public Agent Card and validate it as A2A 1.0.
2. Select a `JSONRPC` interface whose `protocolVersion` is exactly `1.0`; copy
   its `tenant` value into every request, or omit `tenant` when the card omits
   it.
3. Resolve and apply one of the card's declared authentication schemes.
4. Fetch the authenticated extended Agent Card.
5. Verify `capabilities.streaming: true`,
   `capabilities.extendedAgentCard: true`, and an `AgentExtension` with this
   contract's exact URI.
6. Activate the extension and call `externalHarness.capabilities.get`.
7. Require all ten feature booleans in section 3.3 to be `true`, require
   workspace isolation `hard`, and find exact requested mode and model IDs with
   `available: true`.
8. Cache the public and extended card using normal HTTP `ETag`/`Cache-Control`,
   but record the selected card version and descriptor digest on each task.

Any failed step stops before task creation and produces a visible typed error.
Discovery is not best-effort. Hermes MUST NOT use stale capabilities after an
authentication, version, extension, or descriptor mismatch and MUST NOT fall
back to ACPX.

## 5. Harness and identity model

### 5.1 Harness kinds

`harness.kind` is exactly one of:

- `opencode-omo`
- `codex`
- `claude`
- `remote-a2a`

Each adapter instance has an immutable `adapter.instanceId`, generated at
installation and persisted across process restarts. Runtime process IDs are not
agent identities.

### 5.2 Requested identity

Every start request contains:

```json
{
  "modeId": "hephaestus",
  "model": {
    "providerId": "openai",
    "modelId": "gpt-5.6-codex"
  }
}
```

`modeId`, `providerId`, and `modelId` are REQUIRED non-empty strings. A harness
with no user-selectable mode advertises and accepts exactly `modeId: "default"`.
A harness with an opaque provider advertises and accepts a stable provider ID;
it may not use an empty value.

The adapter MUST NOT repair spelling, change case, choose a “close” model, or
use a default when any requested identity field is invalid or unavailable.
Unknown mode/model requests become `TASK_STATE_REJECTED` with `MODE_UNAVAILABLE`
or `MODEL_UNAVAILABLE` before user work begins.

### 5.3 Context identity binding

A context is immutably bound to this tuple on its first accepted task:

`(authenticated principal, agent/tenant, harness kind, mode ID, provider ID,
model ID, workspace ID)`

A later task may reuse the context only with the exact same tuple. A caller that
needs another mode, model, harness, principal, or workspace MUST start a new
context. A mismatch is a request error `CONTEXT_BINDING_MISMATCH`; it is never a
silent session fork.

### 5.4 Mandatory two-source identity proof

The task MUST remain `TASK_STATE_SUBMITTED` while identity is checked. No user
prompt, tool call, or workspace mutation may occur before both checks pass:

1. **Control-plane proof:** query the harness's launch/session/config API and
   read the effective harness kind, mode, provider, model, runtime session ID,
   and runtime version.
2. **Data-plane proof:** run a hidden, no-tools challenge through the actual
   selected runtime. It must return one JSON object containing a fresh 128-bit
   nonce and the same harness/mode/provider/model values. Tool access and user
   workspace write access are disabled for this challenge.

The normalized proof is persisted under task metadata:

```json
{
  "identity": {
    "requested": {
      "modeId": "hephaestus",
      "model": {"providerId": "openai", "modelId": "gpt-5.6-codex"}
    },
    "effective": {
      "modeId": "hephaestus",
      "model": {"providerId": "openai", "modelId": "gpt-5.6-codex"}
    },
    "verificationStatus": "verified",
    "proof": {
      "controlPlane": "verified",
      "dataPlane": "verified",
      "challengeId": "01J...",
      "evidenceSha256": "lowercase-hex",
      "verifiedAt": "2026-09-04T10:00:00.000Z",
      "verifierVersion": "1"
    }
  }
}
```

Raw challenge text is stored only in the authenticated audit record, never in a
public Agent Card. A timeout, malformed response, mismatch, or unavailable
proof endpoint terminates the task as `TASK_STATE_REJECTED` with
`IDENTITY_UNVERIFIED` or `IDENTITY_MISMATCH`. The adapter may not proceed on
advertised configuration alone.

## 6. Workspace allocation and isolation

### 6.1 Workspace request

Every start contains one workspace object:

```json
{
  "id": "01J...",
  "strategy": "git-worktree",
  "isolation": "hard",
  "rootUri": "file:///srv/repos/project",
  "baseRevision": "0123456789abcdef",
  "repository": null
}
```

Rules by strategy:

| Strategy | Required request fields | Allocation owner |
| --- | --- | --- |
| `existing` | `id`, `rootUri` | Hermes/client provides an existing directory. Loopback or a shared trusted mount only. |
| `git-worktree` | `id`, `rootUri`, `baseRevision` | Adapter creates one worktree below its configured allocation root. |
| `scratch` | `id` | Adapter creates an empty task directory. |
| `remote-managed` | `id`, `repository.url`, `repository.revision` | Remote adapter clones/materializes the exact revision in its own allocation root. |

`id` is a client-generated ULID and is the idempotent allocation key.
`rootUri` must be an absolute `file:` URI. Relative paths, other URI schemes,
and revisions that do not resolve exactly are rejected. Credentials must not be
embedded in repository URLs.

The effective workspace metadata contains `id`, `strategy`, `isolation`, an
adapter-local opaque `leaseId`, and the resolved revision. It may include the
canonical `rootUri` only for the owning authenticated principal. Workspace paths
must be redacted from cross-principal errors and logs.

### 6.2 Hard-isolation requirement

`isolation: "hard"` is mandatory for the external-harness path. Canonical-path
validation alone is insufficient. The harness process and every child/tool
process MUST run in an OS-enforced boundary (container, mount namespace,
landlock/bubblewrap equivalent, VM, or per-principal OS ACL) that:

- exposes the assigned workspace as the only writable project tree;
- prevents read, write, directory traversal, symlink escape, and execution in
  every other task's workspace;
- sets process `cwd`, temporary directory, and task-local cache/home locations
  inside the assigned boundary;
- exposes runtime binaries and required dependencies read-only;
- prevents caller-controlled environment variables from weakening the boundary;
- applies to descendants, OMO subagents, shell tools, and resumed processes.

An adapter unable to establish this boundary rejects the task with
`WORKSPACE_ISOLATION_UNAVAILABLE`; it must not downgrade to shared-trusted mode.

Workspace ownership is `(principal, workspaceId)`. Reusing another principal's
ID returns not-found semantics so existence is not disclosed. A context cannot
change workspaces. Parallel contexts never share a writable workspace unless the
caller deliberately supplies the same workspace ID; strict Hermes callers MUST
allocate a unique ID per concurrent lane.

Cancellation and task completion do not delete a workspace in contract v1.
Cleanup is an operator/Hermes retention action after artifacts and diffs have
been collected. This prevents cancellation from destroying forensic evidence.

## 7. Identifier and correlation rules

- `messageId`: 26-character Crockford-base32 ULID generated by the message
  creator; unique per principal.
- `taskId`: ULID generated by the adapter on the first accepted start; durable,
  globally unique within that adapter, and never reused.
- `contextId`: ULID generated by the adapter when omitted on the first start;
  durable and scoped to the authenticated principal.
- `idempotencyKey`: client-generated 16–128 character `[A-Za-z0-9._:-]+` string.
- `eventId`: `<taskId>:<sequence>`.
- Harness runtime/session/process IDs remain adapter-private correlations; they
  never replace `taskId` or `contextId`.

Hermes persists a correlation record:

`Hermes external-run ID -> peer/agent -> A2A taskId -> A2A contextId -> harness
runtime session ID -> workspace ID -> requested/effective identity`

A Hermes Kanban task ID or `delegate_task` ID may appear in an audit-only
`callerCorrelation` field, but it MUST NOT become an A2A task/context ID and does
not transfer lifecycle ownership to A2A.

## 8. Start request and immediate response

Hermes starts all external work with `SendMessage` and
`configuration.returnImmediately: true`. `SendStreamingMessage` MAY be used by
an interactive caller, but the durable task semantics are identical.

Canonical JSON-RPC request:

```json
{
  "jsonrpc": "2.0",
  "id": "01JRPC...",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "01JMSG...",
      "role": "ROLE_USER",
      "parts": [{"text": "Implement the requested change.", "mediaType": "text/plain"}],
      "extensions": ["urn:pysol-dev:a2a:extension:external-harness:v1"]
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain", "application/json"],
      "historyLength": 0,
      "returnImmediately": true
    },
    "metadata": {
      "externalHarness": {
        "contractVersion": "1.0",
        "operation": "start",
        "request": {"idempotencyKey": "caller-01J..."},
        "harness": {"kind": "opencode-omo"},
        "identity": {
          "modeId": "hephaestus",
          "model": {"providerId": "openai", "modelId": "gpt-5.6-codex"}
        },
        "workspace": {
          "id": "01JWS...",
          "strategy": "git-worktree",
          "isolation": "hard",
          "rootUri": "file:///srv/repos/project",
          "baseRevision": "0123456789abcdef"
        }
      }
    }
  }
}
```

If the selected Agent Interface advertises `tenant`, `params.tenant` is also
required and must exactly match it.

Before responding, the adapter MUST atomically persist the request digest,
idempotency record, task, context binding, workspace allocation intent, and
`TASK_STATE_SUBMITTED` status. It then returns the A2A 1.0
`SendMessageResponse` wrapper:

```json
{
  "jsonrpc": "2.0",
  "id": "01JRPC...",
  "result": {
    "task": {
      "id": "01JTASK...",
      "contextId": "01JCTX...",
      "status": {
        "state": "TASK_STATE_SUBMITTED",
        "timestamp": "2026-09-04T10:00:00.000Z"
      },
      "metadata": {
        "externalHarness": {
          "contractVersion": "1.0",
          "lifecycle": {"phase": "queued", "attempt": 1, "reasonCode": null},
          "stream": {"latestSequence": 0, "oldestRetainedSequence": 1}
        }
      }
    }
  }
}
```

The response deadline is two seconds under the acceptance environment. It does
not wait for identity proof or work completion. If durable persistence fails,
the adapter returns `STATE_STORE_UNAVAILABLE` and MUST NOT start the harness.

The start metadata permits exactly these keys under `externalHarness`:
`contractVersion`, `operation`, `request`, `harness`, `identity`, `workspace`,
and optional `callerCorrelation`. The nested required fields are exactly those
shown in the request plus the strategy-specific fields in section 6. Unknown
keys anywhere under this extension metadata are rejected with JSON-RPC invalid
params and a `data.field` path; extension v1 never ignores a misspelled control.
Metadata owned by other negotiated extensions is unaffected.

### 8.1 Core operation wire map

All field names below are A2A 1.0 ProtoJSON names. Each call carries the
authentication, version, tenant (when advertised), and extension headers from
section 3.

| Operation | JSON-RPC request | Success result |
| --- | --- | --- |
| Start | `SendMessage` with no `message.taskId`; optional known `message.contextId`; `configuration.returnImmediately=true`; start metadata from section 8 | `SendMessageResponse.task`, normally `SUBMITTED` |
| Start + attached stream | `SendStreamingMessage` with the same fields | SSE `StreamResponse` frames |
| Get status | `GetTask` with `params.id`, optional `historyLength` | one complete Task snapshot |
| List | `ListTasks` with optional `contextId`, `status`, `pageSize` (1–100), `pageToken`, `historyLength`, `statusTimestampAfter`, and `includeArtifacts` | `{tasks, nextPageToken, pageSize, totalSize}` |
| Subscribe | `SubscribeToTask` with `params.id`; optional HTTP `Last-Event-ID` | SSE `StreamResponse` frames until interrupted/terminal |
| Follow-up | `SendMessage` with matching `message.taskId` and `message.contextId`, user Parts, and section 13 metadata | `SendMessageResponse.task` with the same task ID |
| Refinement | `SendMessage` with the prior `contextId`, no `taskId`, prior IDs in `referenceTaskIds`, and fresh start metadata | a new Task ID in the same context |
| Cancel | `CancelTask` with `params.id` and optional `params.metadata.externalHarness.reason` | terminal Task or a standard A2A task error |

`GetTask`, `ListTasks`, `CancelTask`, and `SubscribeToTask` use `params.id` as
defined by A2A 1.0; adapters may parse legacy `taskId` aliases outside strict
mode, but Hermes never emits them. The `historyLength` value `0` requests no
history. `ListTasks.includeArtifacts` defaults to false; `GetTask` always
returns all current artifacts. `pageToken` is opaque and principal-scoped.

## 9. Idempotency and duplicate delivery

For `operation: "start"` and `operation: "followUp"`, `idempotencyKey` is
mandatory. The adapter scopes it to `(authenticated principal, selected agent,
operation)` and stores a SHA-256 digest of the canonical request.

- Same key and same digest: return the original response/task and perform no new
  side effect.
- Same key and different digest: return `IDEMPOTENCY_CONFLICT`.
- A transport disconnect after submission is resolved by retrying the same key,
  never by inventing a new key.
- Idempotency records live at least as long as the task retention period.

JSON-RPC request IDs are transport correlation only and are not idempotency
keys. A repeated `messageId` with a different idempotency key is rejected as
`DUPLICATE_MESSAGE_ID`.

## 10. Lifecycle state machine

A2A core states remain authoritative. Extension metadata refines them but never
adds enum values.

| User-visible meaning | A2A state | `lifecycle.phase` | Required reason |
| --- | --- | --- | --- |
| Queued/durably accepted | `TASK_STATE_SUBMITTED` | `queued` or `identity-check` | none |
| Running | `TASK_STATE_WORKING` | `starting`, `running`, or `canceling` | none unless canceling |
| Needs user answer/permission | `TASK_STATE_INPUT_REQUIRED` | `input-required` | `INPUT_REQUIRED` |
| Needs harness credentials | `TASK_STATE_AUTH_REQUIRED` | `auth-required` | `AUTH_REQUIRED` |
| Success | `TASK_STATE_COMPLETED` | `completed` | none |
| Runtime/adapter/work failure | `TASK_STATE_FAILED` | `failed` or `dead-runtime` | typed failure code |
| Confirmed cancellation | `TASK_STATE_CANCELED` | `canceled` | `CANCELED_BY_CALLER` |
| Valid request refused before work | `TASK_STATE_REJECTED` | `rejected` | typed rejection code |

“Dead runtime” is represented unambiguously as `TASK_STATE_FAILED`,
`phase: "dead-runtime"`, `reasonCode: "RUNTIME_LOST"` or
`"RUNTIME_CRASHED"`; it must never remain projected as working.

Legal transitions:

```text
SUBMITTED -> WORKING | REJECTED | FAILED | CANCELED
WORKING -> INPUT_REQUIRED | AUTH_REQUIRED | COMPLETED | FAILED | CANCELED
INPUT_REQUIRED -> WORKING | FAILED | CANCELED
AUTH_REQUIRED -> WORKING | FAILED | CANCELED
COMPLETED | FAILED | CANCELED | REJECTED -> no transition
```

An interrupted-to-interrupted change must pass through a persisted `WORKING`
transition. The first persisted terminal state wins and is immutable, including
artifacts and effective identity. A later event may be logged as a late race but
cannot change the task.

The adapter MUST persist a transition and its event in one database transaction
before publishing it. Timestamps are UTC RFC 3339 with millisecond precision and
must not move backward for one task.

## 11. Task snapshots and status queries

`GetTask` is the source of truth after any timeout, disconnect, or process
restart. It returns the latest persisted Task plus:

```json
{
  "externalHarness": {
    "contractVersion": "1.0",
    "harness": {
      "kind": "opencode-omo",
      "adapterVersion": "1.2.3",
      "runtimeVersion": "1.0.0"
    },
    "identity": {"requested": {}, "effective": {}, "verificationStatus": "verified"},
    "workspace": {
      "id": "01JWS...",
      "strategy": "git-worktree",
      "isolation": "hard",
      "leaseId": "opaque",
      "revision": "0123456789abcdef"
    },
    "lifecycle": {
      "phase": "running",
      "attempt": 1,
      "reasonCode": null,
      "retryable": false,
      "cancelRequested": false,
      "lastHeartbeatAt": "2026-09-04T10:00:05.000Z"
    },
    "stream": {"latestSequence": 42, "oldestRetainedSequence": 1},
    "correlation": {"harnessSessionId": "redacted-or-opaque"}
  }
}
```

The following task metadata is always present: `contractVersion`,
`harness.kind`, `identity.requested`, `identity.verificationStatus`,
`workspace.id`, `workspace.strategy`, `workspace.isolation`,
`lifecycle.phase`, `lifecycle.attempt`, `lifecycle.reasonCode`,
`lifecycle.retryable`, `lifecycle.cancelRequested`, `stream.latestSequence`,
and `stream.oldestRetainedSequence`. `identity.effective`, `identity.proof`,
workspace `leaseId`/`revision`, runtime version, and harness session correlation
appear as soon as they are known and then remain immutable. Unknown additive
response metadata is ignored by clients.

`ListTasks` must implement A2A 1.0 filters and pagination. Results are scoped to
the authenticated principal and selected agent/tenant. A task outside that scope
returns `TaskNotFoundError`, not permission-denied, to avoid existence leakage.

Nonterminal tasks are retained without TTL. After terminal state, task snapshots
and idempotency rows are retained for at least 30 days and event rows for at
least seven days. Actual configured values are advertised in capabilities.

## 12. Streaming, ordering, and reconnect

### 12.1 Event ledger

Every `TaskStatusUpdateEvent` and `TaskArtifactUpdateEvent` receives a strictly
increasing per-task sequence beginning at 1:

```json
{
  "externalHarness": {
    "stream": {
      "sequence": 17,
      "eventId": "01JTASK...:17",
      "blockType": "text"
    }
  }
}
```

The same values are placed in the A2A event's `metadata`. Each SSE frame also
uses `id: <eventId>`. Delivery is at-least-once; persistence and sequence
assignment are exactly-once. Clients deduplicate by `(taskId, sequence)`.
Duplicate delivery must be byte-equivalent after canonical JSON serialization.

Allowed `blockType` values are `text`, `reasoning`, `tool_call`, and `status`.
Text/reasoning use text Parts. Tool calls use a data Part with keys `callId`,
`tool`, `status`, `title`, `input`, `output`, and `error`; absent values are
omitted. Secrets and absolute paths are redacted before persistence and egress.

One artifact keeps a stable `artifactId` across chunks. `append: true` appends
to prior content; `lastChunk: true` closes that artifact. Final artifacts are
persisted before the terminal status event. No event may be emitted after a
terminal status event.

### 12.2 Initial streaming request

`SendStreamingMessage` emits:

1. the current Task snapshot;
2. ordered events after that snapshot's `latestSequence`;
3. live events until terminal or interrupted state;
4. stream close, with no synthetic empty data frame.

SSE keepalive comments have no sequence and are not task events.

### 12.3 Reconnect/resubscribe algorithm

On any broken stream, Hermes MUST:

1. call `GetTask`;
2. stop if the state is terminal;
3. surface the prompt and wait for follow-up if the state is interrupted;
4. otherwise call `SubscribeToTask` with HTTP `Last-Event-ID` set to the last
   fully processed `<taskId>:<sequence>`.

The adapter atomically captures the replay/live boundary, replays all persisted
events with sequence greater than the cursor, then emits live events. Events may
be redelivered but none may be skipped or reordered.

Without `Last-Event-ID`, `SubscribeToTask` first emits the current Task snapshot
whose metadata includes `latestSequence`, then emits only newer live events.
This is a state subscription, not a full-history request.

For a terminal task, `SubscribeToTask` returns the A2A
`UnsupportedOperationError`; the client already has the terminal snapshot from
`GetTask`. A cursor older than `oldestRetainedSequence - 1` returns
`EVENT_CURSOR_EXPIRED`; Hermes then obtains the complete Task/artifacts through
`GetTask` and marks incremental stream reconstruction unavailable rather than
pretending it saw every event.

## 13. Follow-up and interactive input

A nonterminal task accepts follow-up through `SendMessage` with the same
`message.taskId` and `message.contextId`.

Canonical extension metadata:

```json
{
  "externalHarness": {
    "contractVersion": "1.0",
    "operation": "followUp",
    "request": {"idempotencyKey": "caller-01J-followup"},
    "followUp": {
      "kind": "answer",
      "interactionId": "01JINT..."
    }
  }
}
```

`followUp.kind` is:

- `answer`: valid only in `INPUT_REQUIRED` or `AUTH_REQUIRED`; `interactionId`
  must match the pending interaction. It transitions the same task through
  `WORKING` and resumes it.
- `steer`: valid only in `SUBMITTED` or `WORKING`; it is durably queued and
  delivered at the next harness safe point while the same task remains active.

Follow-up requests MUST omit `harness`, `identity`, and `workspace`; those values
are inherited from the immutable context binding. A duplicate follows section
9. A task may have only one pending interactive request at a time.

An interrupted status message includes:

```json
{
  "externalHarness": {
    "interaction": {
      "id": "01JINT...",
      "type": "question",
      "expiresAt": "2026-09-04T10:10:00.000Z",
      "responseSchema": {"type": "string"}
    }
  }
}
```

`type` is `question`, `permission`, or `authentication`. Expiry without a valid
answer terminates the task as `FAILED` with `INPUT_EXPIRED` or `AUTH_EXPIRED`.
Transport authentication failure is an HTTP/JSON-RPC error, never
`TASK_STATE_AUTH_REQUIRED`.

Once a task is terminal it is immutable. A refinement starts a new task with the
same `contextId`, no `taskId`, and the old ID in `referenceTaskIds`; it uses a
new idempotency key and must preserve the context binding.

## 14. Cancellation

`CancelTask` is idempotent in this contract:

1. Unknown/out-of-scope task: A2A `TaskNotFoundError`.
2. Already `CANCELED`: return the unchanged task.
3. Other terminal state: A2A `TaskNotCancelableError`.
4. `SUBMITTED` before dispatch: atomically mark `CANCELED`; no process starts.
5. Active task: persist `cancelRequested: true`, set phase `canceling`, invoke
   the harness's real interrupt/abort, and await confirmation for at most the
   advertised `cancelConfirmTimeoutMs`.
6. Confirmed process/session abort: persist `CANCELED`.
7. Completion wins the race before cancel persistence: retain `COMPLETED`.
8. Abort error, timeout, or contradictory “nothing to cancel” while the runtime
   is still projected active: perform a bounded hard-kill/reap fallback for the
   task's process tree. If that fallback is confirmed, persist `CANCELED`; if it
   is not confirmed, revoke the execution lease, quarantine the workspace,
   raise an operator-health alert, and terminate the task as `FAILED` with
   `CANCEL_FAILED` and `runtimeMayStillBeActive: true`. Never report `CANCELED`
   without runtime confirmation and never leave a stale `WORKING` projection.

A cancellation response returns only after one terminal outcome above is
persisted. Killing an SSE connection is not cancellation. Cancellation does not
delete workspace data or completed artifacts.

## 15. Durable state and restart recovery

A conforming adapter uses a transactional store, not process-memory maps, for at
least these logical tables:

- tasks: IDs, principal/agent scope, state, phase, reason, retryability,
  identity, workspace, timestamps, terminal snapshot, row version;
- contexts: immutable binding tuple and harness session ID;
- requests: idempotency key, operation, canonical digest, response/task ID;
- events: `(taskId, sequence)` primary key, event ID, canonical payload;
- workspace leases: owner, strategy, canonical boundary, revision, state;
- runtime attempts: attempt number, adapter instance, runtime/session/process
  handles, start intent, heartbeat, exit code/signal;
- interactions: task, interaction ID/type/schema, expiry, resolution.

Task creation and the first idempotency record are one transaction. Each state
transition and event insertion are one transaction. Store failure is loud and
prevents the corresponding external side effect.

Workers hold renewable execution leases. The default heartbeat is five seconds
and lease expiry is 15 seconds; configured values must be in the capability
descriptor. After adapter restart, it scans every nonterminal task:

- `SUBMITTED`, no recorded dispatch: reclaim and dispatch once.
- `SUBMITTED`, ambiguous prior dispatch: query the harness by task/request ID;
  reattach if proven, otherwise fail `RECOVERY_AMBIGUOUS` rather than duplicate.
- `WORKING`: query the persisted harness session/process. Reattach monitoring if
  live, reconcile a proven terminal result, or fail with phase `dead-runtime`
  and `RUNTIME_LOST`/`RUNTIME_CRASHED`.
- `INPUT_REQUIRED` or `AUTH_REQUIRED`: restore the pending interaction and
  expiry timer without starting a new turn.
- `cancelRequested: true`: resume the cancellation algorithm before any work.

A runtime process exit, SIGTERM, lost session, malformed event stream, or adapter
restart can never be represented indefinitely as running. Recovery increments
`lifecycle.attempt` only when a new runtime attempt is intentionally created; a
monitor reattachment does not.

## 16. Authentication, authorization, and data handling

- Even on loopback, the production Hermes-to-adapter link requires a bearer
  token mapped to a stable principal. Anonymous loopback is allowed only in
  isolated tests and cannot pass cutover acceptance.
- Non-loopback uses TLS plus a card-declared bearer/OAuth2/mTLS scheme. Static
  bearer tokens are acceptable for the initial controlled deployment and must
  be rotatable without changing task IDs.
- Principal identity comes only from validated transport credentials. Body
  fields, task IDs, context IDs, and workspace IDs never assert identity.
- Every method, event replay, task lookup, context, interaction, and workspace
  operation is owner-scoped. Unauthorized objects use not-found semantics.
- Secrets, bearer values, repository credentials, raw environment values, and
  unrestricted absolute paths never enter Task metadata, artifacts, event
  payloads, status messages, or user-facing errors.
- Inbound instructions are framed as untrusted peer content. External peers
  cannot issue Hermes operator slash commands.
- Request size limits, per-principal rate limits, timeouts, and audit records are
  mandatory and advertised where relevant.
- Audit records include principal, selected Agent Card/descriptor digest,
  request/idempotency key, task/context/workspace IDs, requested/effective
  identity, state transitions, event sequence, runtime attempt/exit, and
  cancellation outcome. Audit writes are durable before success is reported.

## 17. Error contract

### 17.1 Request-level errors

Malformed JSON-RPC, invalid params, method-not-found, A2A version errors, task
not found, and task not cancelable use standard JSON-RPC/A2A error classes. The
extension reserves these codes outside A2A 1.0's current `-32001..-32009`
error-code block:

| Code | `reasonCode` | Retryable |
| ---: | --- | --- |
| `-32100` | `CONTRACT_UNSUPPORTED` | false |
| `-32101` | `CAPABILITY_UNSUPPORTED` | false |
| `-32102` | `IDEMPOTENCY_CONFLICT` | false |
| `-32103` | `DUPLICATE_MESSAGE_ID` | false |
| `-32104` | `INVALID_TRANSITION` | false |
| `-32105` | `EVENT_CURSOR_EXPIRED` | false |
| `-32106` | `CONTEXT_BINDING_MISMATCH` | false |
| `-32107` | `WORKSPACE_CONFLICT` | false |
| `-32108` | `STATE_STORE_UNAVAILABLE` | true |
| `-32109` | `RUNTIME_UNAVAILABLE` | true |

Every extension error has this data shape:

```json
{
  "reasonCode": "IDEMPOTENCY_CONFLICT",
  "retryable": false,
  "taskId": "optional",
  "contextId": "optional",
  "operation": "start",
  "detail": "sanitized human-readable detail"
}
```

HTTP authentication uses 401; authenticated but unauthorized method use uses
403; rate limiting uses 429 with `Retry-After`; malformed transport body uses
400. JSON-RPC application errors otherwise use HTTP 200.

### 17.2 Task-level failures and rejections

After a Task exists, execution failures are terminal Task states, not successful
empty replies. `TASK_STATE_REJECTED` is used before user work begins;
`TASK_STATE_FAILED` is used after acceptance or for infrastructure/runtime
failure.

Required task `reasonCode` vocabulary:

```text
INPUT_REQUIRED
AUTH_REQUIRED
MODE_UNAVAILABLE
MODEL_UNAVAILABLE
IDENTITY_UNVERIFIED
IDENTITY_MISMATCH
WORKSPACE_DENIED
WORKSPACE_ISOLATION_UNAVAILABLE
INPUT_EXPIRED
AUTH_EXPIRED
UPSTREAM_AUTH_REQUIRED
UPSTREAM_UNREACHABLE
UPSTREAM_PROTOCOL_ERROR
UPSTREAM_BACKPRESSURE
RUNTIME_START_FAILED
RUNTIME_CRASHED
RUNTIME_LOST
RECOVERY_AMBIGUOUS
PROCESS_EXIT_NONZERO
TIMEOUT
CANCEL_FAILED
STREAM_CORRUPT
STATE_STORE_FAILED
CANCELED_BY_CALLER
```

Default state and retryability are fixed as follows; an adapter may add detail
but may not change these defaults:

| Reason code | State | Retryable |
| --- | --- | :---: |
| `INPUT_REQUIRED` | `INPUT_REQUIRED` | false until answered |
| `AUTH_REQUIRED`, `UPSTREAM_AUTH_REQUIRED` | `AUTH_REQUIRED` | false until credentials change |
| `MODE_UNAVAILABLE`, `MODEL_UNAVAILABLE`, `IDENTITY_UNVERIFIED`, `IDENTITY_MISMATCH`, `WORKSPACE_DENIED`, `WORKSPACE_ISOLATION_UNAVAILABLE` | `REJECTED` | false |
| `UPSTREAM_BACKPRESSURE` | `REJECTED` | true |
| `INPUT_EXPIRED`, `AUTH_EXPIRED`, `UPSTREAM_PROTOCOL_ERROR`, `RECOVERY_AMBIGUOUS`, `PROCESS_EXIT_NONZERO` | `FAILED` | false |
| `UPSTREAM_UNREACHABLE`, `RUNTIME_START_FAILED`, `RUNTIME_CRASHED`, `RUNTIME_LOST`, `TIMEOUT`, `CANCEL_FAILED`, `STREAM_CORRUPT`, `STATE_STORE_FAILED` | `FAILED` | true |
| `CANCELED_BY_CALLER` | `CANCELED` | false |

Task failure metadata includes `retryable`, `attempt`, and sanitized diagnostics
(`exitCode`, `signal`, `upstreamStatus`, or `lastEventSequence` when known). A
failure with an unknown cause uses `UPSTREAM_PROTOCOL_ERROR`, never a blank
reason. Hermes surfaces the state, reason code, retryability, peer, task ID, and
one safe detail line in the invoking chat/terminal; it must not convert failure
to normal prose that looks successful.

## 18. Harness-specific mappings

All adapters expose the common contract above. Provider-private APIs remain
behind the adapter.

### 18.1 OpenCode OMO

- `modeId` maps to the exact OMO agent/persona/config-option ID (for example,
  `hephaestus`), not an ACPX lane name.
- Model maps to OpenCode `providerID`/`modelID` and must be applied before the
  data-plane proof. The upstream adapter's current “invalid/partial means use
  default” behavior is nonconforming and must become rejection.
- A2A `contextId` maps through the durable principal/context binding to one
  OpenCode session ID. Existing `metadata.shared.session.id` and
  `metadata.shared.model` may remain internal compatibility helpers but the
  common external request is `metadata.externalHarness`.
- Workspace ID maps to an OpenCode workspace/worktree and the process sandbox.
- Cancel maps to OpenCode session abort plus process confirmation. Emitting
  canceled before abort confirmation is nonconforming.
- OMO child/subtask execution remains OpenCode/OMO internal behavior; those
  child IDs are not promoted to Hermes Kanban or `delegate_task` IDs.

### 18.2 Codex

- The adapter owns Codex process/thread/session creation and resume.
- `modeId` is one exact advertised Codex interaction/sandbox mode; adapters with
  no selectable mode expose only `default`.
- Requested model is passed explicitly on every fresh/resumed runtime and is
  verified through control-plane state plus the no-tools challenge.
- Codex JSONL/events are normalized into A2A artifact/status events and persisted
  before egress.
- Cancel must interrupt and reap the real Codex runtime or return `CANCEL_FAILED`.

### 18.3 Claude

- The adapter owns Claude session UUID creation/resume and stream-json parsing.
- `modeId` maps to one exact advertised Claude permission/interaction mode.
- Requested Claude model is explicit on fresh and resumed runs and receives the
  same two-source proof.
- Permission prompts become `INPUT_REQUIRED` interactions; credentials become
  `AUTH_REQUIRED`.
- Cancel interrupts and reaps the actual Claude process before `CANCELED`.

### 18.4 Remote non-Hermes A2A agent

- The remote endpoint itself must advertise and implement this extension.
- If it cannot expose an exact mode/model catalog, two-source identity proof,
  durable/replayable tasks, and hard server-managed workspace isolation, Hermes
  rejects it from the external-harness path.
- Server-managed workspace allocation uses `remote-managed`; local `file:` paths
  are not assumed to exist remotely.
- Generic A2A interaction remains available outside this strict path but does not
  satisfy ACPX replacement tests.

## 19. ACPX migration boundary

| ACPX responsibility | A2A replacement | Not replaced / notes |
| --- | --- | --- |
| Agent registry and command spawn | Configured A2A peer + Agent Card + authenticated capability descriptor | Adapter privately owns harness launch commands. |
| `ensureSession()` and encoded runtime handle | Durable A2A `contextId` binding plus adapter-private harness session ID | No ACPX handle crosses the boundary. |
| Persistent/oneshot mode | Context reuse for persistent work; a new context for one-shot work | This is session lifetime, not `identity.modeId`. |
| `startTurn()` / prompt request ID | `SendMessage(returnImmediately=true)` + required idempotency key | A2A `taskId` is the durable unit. |
| Event iterable and terminal result promise | `SubscribeToTask`/SSE + `GetTask` terminal snapshot | Event ledger supplies reconnect ordering. |
| `getStatus()` | `GetTask`, `ListTasks`, and capability descriptor | Runtime-lost is a typed failed task, never a queue-owner guess. |
| Mode/config/model set and replay | Immutable requested identity, exact adapter application, two-source proof | No silent replay/default fallback. |
| Working directory | Workspace allocation + hard sandbox lease | Path validation alone is insufficient. |
| Prompt/steer | A2A start and `followUp.kind=steer` | Same task for active steering. |
| Permission/elicitation | A2A interrupted states + typed interaction + `followUp.kind=answer` | Transport auth remains transport-level. |
| Cancel | Idempotent `CancelTask` with confirmed real abort | “Nothing to cancel” while working is failure. |
| Session/process close | Adapter retention/reaping and operator workspace cleanup | Task terminal state is immutable; no dual protocol cleanup. |
| Queue owner and named lanes | Durable tasks, principal/context/workspace binding, execution lease | Named lane is a Hermes UI correlation, not protocol identity. |

Hermes internal child orchestration, Kanban, Bot Mode, and peer/profile messaging
are explicitly outside this table and remain unchanged.

## 20. Interoperability matrix and ACPX disable gate

### 20.1 Required matrix

Every non-`N/A` cell must be automated. No skip or xfail counts as a pass.

| Scenario | Hermes client + OMO adapter | Hermes + Codex adapter | Hermes + Claude adapter | Hermes + remote reference agent | Official A2A Python SDK + each adapter |
| --- | :---: | :---: | :---: | :---: | :---: |
| Public and extended discovery | PASS | PASS | PASS | PASS | PASS |
| Exact mode/model accepted | PASS | PASS | PASS | PASS | N/A for extension helper only |
| Unknown mode/model rejected, no process | PASS | PASS | PASS | PASS | PASS wire parsing |
| Immediate durable start | PASS | PASS | PASS | PASS | PASS |
| Same-key duplicate returns same task | PASS | PASS | PASS | PASS | PASS |
| Same-key/different-body conflict | PASS | PASS | PASS | PASS | PASS |
| Ordered text/tool/reasoning stream | PASS | PASS | PASS | PASS | PASS core stream |
| Disconnect and cursor replay | PASS | PASS | PASS | PASS | PASS without custom cursor; Hermes tests cursor |
| Adapter restart during queued/running | PASS | PASS | PASS | PASS | PASS GetTask |
| Runtime crash/SIGTERM becomes failed | PASS | PASS | PASS | PASS | PASS GetTask |
| Input-required answer | PASS | PASS | PASS | PASS | PASS core state |
| Working-task steer | PASS | PASS | PASS | PASS | PASS wire parsing |
| Cancel before start | PASS | PASS | PASS | PASS | PASS |
| Cancel while running and repeated cancel | PASS | PASS | PASS | PASS | PASS |
| Completion/cancel race, first terminal wins | PASS | PASS | PASS | PASS | PASS |
| Two concurrent hard-isolated workspaces | PASS | PASS | PASS | PASS | N/A |
| Auth/ownership negative tests | PASS | PASS | PASS | PASS | PASS |
| Explicit error propagation to Hermes UI | PASS | PASS | PASS | PASS | N/A |
| Generic Hermes/Kanban regression suite | PASS | PASS | PASS | PASS | N/A |

### 20.2 Objective pass/fail criteria

ACPX may be disabled only when all criteria below are evidenced on the exact
candidate commits:

1. **Protocol:** public/extended cards and every core method parse with the
   official A2A Python SDK; extension fixtures validate exact schemas and reject
   unknown required values.
2. **Immediate durability:** 100 start attempts each return in at most two
   seconds and their task is queryable after an immediate adapter `SIGKILL` and
   restart.
3. **Idempotency:** 100 duplicate retries create exactly 100 tasks and exactly
   100 harness starts, not 200. Every conflicting body is rejected.
4. **Identity:** 20 fresh and 20 resumed runs per harness report the exact
   requested/effective mode and model and pass both proof sources. One injected
   mismatch produces zero user-work tool calls and a rejected task.
5. **Lifecycle:** a state-transition property test covers every legal edge and
   rejects every illegal edge. Terminal snapshots remain byte-equivalent after
   late completion/cancel/crash events.
6. **Restart:** kill the adapter once in `SUBMITTED`, once in `WORKING`, once in
   each interrupted state, and once during cancellation. Each task either
   reattaches or reaches a typed terminal failure within 30 seconds; none remains
   falsely working.
7. **Streaming:** for streams of at least 1,000 events, disconnect after each of
   25 selected sequence boundaries. Reconstructed event IDs and artifact bytes
   exactly match the uninterrupted control, with no gap or reordering; duplicate
   frames deduplicate cleanly.
8. **Cancellation:** cancel-before-dispatch starts no process. Active cancel
   confirms the target process/session is gone within the advertised timeout.
   Repeated cancel returns the same canceled task. Injected abort failure becomes
   `FAILED/CANCEL_FAILED`, never canceled or stale working.
9. **Isolation:** run two malicious concurrent tasks in different workspaces.
   Each attempts path traversal, symlink escape, direct absolute-path read/write,
   and execution from the other's tree. Every attempt is denied and file hashes
   in the other workspace remain unchanged.
10. **Auth:** missing, invalid, expired, wrong-principal, and wrong-tenant
    credentials cannot discover private capabilities or read/control another
    task/context/workspace. Logs and responses contain no test secret.
11. **Failure visibility:** inject unavailable model, unavailable mode, state
    store outage, runtime start failure, nonzero exit, SIGTERM, malformed stream,
    network timeout, and rate limit. The invoking Hermes terminal/chat receives
    the expected state, reason code, retryability, task ID when one exists, and
    a safe detail line.
12. **Boundary regression:** targeted and full relevant Hermes tests show
    `delegate_task`, Bot Mode, profiles/peers, and Kanban still use their existing
    internal paths. A routing spy proves no internal child/Kanban task invokes
    the A2A external-harness client.
13. **No fallback:** with A2A disabled, unreachable, or nonconforming, every
    external-harness request fails loudly. A process/network spy proves ACPX was
    not invoked.
14. **Independent E2E:** an independent reviewer runs the full matrix twice from
    fresh state using pinned commits and retains cards, request/response
    fixtures, ordered event logs, process-exit evidence, workspace hash reports,
    and test output. Both runs pass with no skip/xfail/flaky retry.

Only after the independent artifacts exist may the cutover change disable the
`acpx-opencode` plugin/runtime references and select A2A for all external
harnesses. The same change records a rollback branch and configuration snapshot.
A rollback restores ACPX as the sole external route; it never enables both
routes for one task.

## 21. Minimum implementation split

The downstream implementation may use different module names, but ownership is
fixed:

- **Hermes:** strict discovery/preflight; durable external-run correlation;
  start/get/list/subscribe/follow-up/cancel surfaces; stream cursor handling;
  user-visible typed failures; A2A-only routing selector; no changes to internal
  child/Kanban execution.
- **Each harness adapter:** capability descriptor; exact mode/model application;
  two-source proof; transactional task/event/runtime/context/workspace store;
  hard sandbox; process/session supervision; follow-up delivery; confirmed
  cancellation; restart reconciliation.
- **Shared fixtures:** request/response JSON, lifecycle transition table, reason
  code catalog, event replay traces, capability descriptors, and negative auth
  cases. Both Hermes and adapter suites consume the same fixtures.
- **Acceptance harness:** process kill/restart controls, two-workspace attacker,
  official A2A SDK probe, and artifact collection. It is independent of ACPX.

A component is not conforming when it merely returns the right JSON while the
real harness did not run, ran with another identity, escaped its workspace,
failed without a terminal task, or remained projected as working after death.
