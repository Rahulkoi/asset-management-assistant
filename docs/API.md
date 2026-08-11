# API Documentation

Base URL `http://localhost:8000` · Interactive OpenAPI docs at
[`/docs`](http://localhost:8000/docs) · Machine-readable schema at `/openapi.json`

```bash
make run-api
```

The API has two halves, and the split is the point:

- **`/chat` and `/chat/stream`** are the agent. The model decides which tools to
  call. Writes are two-phase — preview, then confirm on a later turn.
- **Everything else** is the same capability as plain REST with no model in the
  path. The deterministic half is fully testable and integrable without an LLM.

No authentication. Every request is trusted as `actor` — see
[Known limitations](../README.md#known-limitations).

---

## Contents

| Endpoint | Method | Purpose |
|---|---|---|
| [`/chat`](#post-chat) | POST | Ask the agent |
| [`/chat/stream`](#post-chatstream) | POST | Same, as Server-Sent Events |
| [`/sessions/{session_id}/reset`](#post-sessionssession_idreset) | POST | Clear history and pending confirmations |
| [`/assets/{asset_code}`](#get-assetsasset_code) | GET | Look up one asset |
| [`/assets`](#get-assets) | GET | Filter assets |
| [`/assets`](#post-assets) | POST | Create an asset |
| [`/employees/{name}`](#get-employeesname) | GET | Profile, manager, reports, holdings |
| [`/recommendations`](#get-recommendations) | GET | Available stock only |
| [`/transfers`](#post-transfers) | POST | Transfer an asset |
| [`/audit`](#get-audit) | GET | Mutation log, newest first |
| [`/healthz`](#get-healthz) | GET | Component status |

---

## POST `/chat`

Ask the agent a question. It selects its own tools and returns a grounded
answer alongside everything it did to produce it.

**Request**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `message` | string | ✅ | — | The user's question |
| `session_id` | string | | `"default"` | Conversation key. Same id ⇒ history and referents carry over, so `"who is using it?"` resolves |
| `actor` | string | | `"api"` | Recorded on any mutation in the audit log |

```bash
curl -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"session_id":"demo","message":"Who is using AST1002 and who is their manager?"}'
```

**Response** — `ChatResponse`

| Field | Type | Notes |
|---|---|---|
| `reply` | string | The natural-language answer |
| `session_id` | string | Echoed back |
| `trace_id` | string | Correlates with the row in `data/traces.jsonl` |
| `tools_used` | string[] | Tool names, in call order |
| `tool_calls` | ToolSpan[] | `tool`, `arguments`, `ok`, `error_code`, `duration_ms` |
| `citations` | object[] | Policy sources, when `search_policy` ran |
| `pending_confirmation` | object \| null | Present when a write is awaiting approval — see [the write flow](#the-two-phase-write-flow) |
| `guardrails` | GuardrailOut[] | Controls that fired: `stage`, `rule`, `action`, `detail` |
| `usage` | object | `input_tokens`, `output_tokens` |
| `outcome` | string | `ok`, `rate_limited`, `timeout`, … |
| `duration_ms` | number | Wall-clock for the turn |

```json
{
  "reply": "AST1002 is assigned to Amit Kumar. Amit Kumar's manager is Priya Singh.",
  "session_id": "demo",
  "trace_id": "3f9c…",
  "tools_used": ["lookup_asset"],
  "tool_calls": [
    {"tool": "lookup_asset", "arguments": {"asset_code": "AST1002"},
     "ok": true, "error_code": null, "duration_ms": 1.2}
  ],
  "citations": [],
  "pending_confirmation": null,
  "guardrails": [],
  "usage": {"input_tokens": 3964, "output_tokens": 208},
  "outcome": "ok",
  "duration_ms": 1444
}
```

### The two-phase write flow

`transfer_asset` and `add_asset` never commit on first call. The first turn
returns a preview and a `pending_confirmation`; the change is committed only
when a **later user turn** approves it.

```
POST /chat  {"session_id":"s1", "message":"Transfer AST1002 to Priya Singh"}
  → reply: "This would move AST1002 from Amit Kumar to Priya Singh.
            Nothing has changed yet — shall I go ahead?"
  → pending_confirmation: {tool, summary, preview, confirm_token}
  → database: UNCHANGED

POST /chat  {"session_id":"s1", "message":"yes"}
  → reply: "Done. AST1002 is now assigned to Priya Singh."
  → database: committed, audit row written
```

The token is minted server-side, single-use, expires in five minutes, and is
bound to both the session and a hash of the arguments. It stays inert until a
new user turn releases it, so the model cannot preview and redeem a write in one
turn. Approval for one change cannot be spent on a different one.

**Release requires an actual approval, and the default is deny.** A next turn is
necessary but not sufficient — the content of the reply decides:

| Next message on that session | Pending preview |
|---|---|
| `"yes"`, `"go ahead"`, `"confirm"` | released, and the commit may proceed |
| `"no"`, `"cancel that"`, `"stop"` | **discarded** |
| a question, a new instruction, anything else | **discarded** |

So a client cannot approve a write by sending unrelated traffic, and a preview
the user ignored cannot be redeemed three turns later. The decision appears in
the response's `guardrails` array as `user_approved`, `user_declined` or
`no_decision`, so a caller can tell what happened without guessing:

```json
{"stage": "confirmation", "rule": "user_declined",
 "action": "discarded", "detail": "discarded 1 pending write(s); user declined"}
```

If you are driving the API programmatically, do not rely on phrasing — send a
literal `"yes"` to commit, and treat any `pending_confirmation` you did not
answer as void.

Note the deliberate asymmetry with [`POST /transfers`](#post-transfers), which
commits immediately: an explicit API call *is* the confirmation. The two-phase
gate exists for the path where intent was inferred from natural language.

---

## POST `/chat/stream`

Identical request body. Responds as `text/event-stream`:

| Event | Payload |
|---|---|
| `tool_start` | `{tool, arguments}` |
| `tool_end` | `{tool, ok, duration_ms}` |
| `result` | The full `ChatResponse` |

```bash
curl -N -X POST localhost:8000/chat/stream \
  -H 'content-type: application/json' \
  -d '{"message":"Find an available laptop in Bangalore"}'
```

---

## POST `/sessions/{session_id}/reset`

Clears conversation history, referents and any pending confirmation for that
session. Used by the UI's reset button.

---

## GET `/assets/{asset_code}`

Case-insensitive exact fetch. `404` if the code is unknown.

```bash
curl localhost:8000/assets/AST1002
```

```json
{
  "asset_code": "AST1002", "asset_name": "Lenovo ThinkPad X1",
  "category": "Laptop", "location": "Bangalore",
  "status": "Assigned", "condition": "New",
  "purchase_date": "2025-01-26", "assigned_to": "Amit Kumar",
  "source": "xlsx"
}
```

`source` is `xlsx` for the 21 rows supplied in the spreadsheet and `synthetic`
for the 14 added by the seed — see
[Data provenance](../README.md#data-provenance--read-this-first).

## GET `/assets`

All filters optional and combinable; unfiltered returns everything up to `limit`.

| Query param | Type | Default |
|---|---|---|
| `category` | string | — |
| `location` | string | — |
| `status` | string — `Assigned` · `Available` · `In Repair` | — |
| `employee_name` | string | — |
| `limit` | integer | server default |

```bash
curl "localhost:8000/assets?category=Printer&location=Mumbai"
```

Returns `AssetOut[]`. An empty match is `200` with `[]`, not `404`.

## POST `/assets`

Creates an asset and returns the created `AssetOut`. Commits immediately.

| Field | Type | Required |
|---|---|---|
| `asset_name` | string | ✅ |
| `category` | string | ✅ |
| `location` | string | ✅ |
| `assign_to_employee` | string \| null | |
| `purchase_date` | string (`YYYY-MM-DD`) \| null | |
| `condition` | string | defaults server-side |
| `actor` | string | defaults to `api` |

The asset code is allocated server-side. `assign_to_employee` must name a known
employee — an unknown name is `404`, not a silently created person.

---

## GET `/employees/{name}`

```bash
curl "localhost:8000/employees/Amit%20Kumar"
```

Returns `employee_id`, `full_name`, `email`, `department`, `location`,
`manager_name`, `direct_reports[]`, `assets_held[]`. This is what makes the
multi-step question — *"who is using AST1002, and who is that employee's
manager?"* — answerable. `404` if unknown.

---

## GET `/recommendations`

Available stock only. Ranked exact-city → same-region → elsewhere, newest first.
Assigned and in-repair assets are excluded **in SQL**, not by prompt — so an
in-repair laptop sitting in Bangalore never appears.

| Query param | Type | Required |
|---|---|---|
| `category` | string | ✅ |
| `location` | string | |
| `limit` | integer | |

```bash
curl "localhost:8000/recommendations?category=Laptop&location=Bangalore"
```

```json
[{"asset_code": "AST1022", "asset_name": "MacBook Pro M3",
  "category": "Laptop", "location": "Bangalore", "condition": "New",
  "purchase_date": "2025-09-02",
  "reason": "Available in Bangalore; condition New, purchased 2025-09-02"}]
```

`reason` is generated by the tool, not the model — the ranking is explainable
without trusting the LLM's narration of it.

---

## POST `/transfers`

Reassigns an asset. Commits immediately (see
[the asymmetry note](#the-two-phase-write-flow)).

| Field | Type | Required |
|---|---|---|
| `asset_code` | string | ✅ |
| `to_employee` | string | ✅ |
| `reason` | string \| null | |
| `actor` | string | defaults to `api` |
| `idempotency_key` | string \| null | replaying the same key will not transfer twice |

```bash
curl -X POST localhost:8000/transfers \
  -H 'content-type: application/json' \
  -d '{"asset_code":"AST1002","to_employee":"Priya Singh","reason":"team move"}'
```

`404` if the asset or employee is unknown. Replaying a previously-used
`idempotency_key` returns `409` and does not transfer twice.

This endpoint **does not** refuse an `In Repair` asset — it is the raw
capability layer, and the eligibility rule lives in policy, which the agent
consults on the `/chat` path. A caller going straight to REST is assumed to have
made that decision themselves. Worth knowing if you compare it against
`/recommendations`, which *does* exclude in-repair stock.

---

## GET `/audit`

Every mutation, newest first.

| Query param | Type |
|---|---|
| `asset_code` | string |
| `limit` | integer |

Returns `AuditEntryOut[]`: `id`, `asset_code`, `action`, `from_employee`,
`to_employee`, `reason`, `actor`, `transferred_at`. Writes through `/chat` and
through REST land in the same log.

---

## GET `/healthz`

```json
{
  "status": "ok", "database": true,
  "policy_index": true, "policy_chunks": 46,
  "embeddings": false,
  "llm_provider": "openai_compat", "llm_configured": true,
  "model": "openai/gpt-oss-120b"
}
```

`embeddings: false` means retrieval is running **lexical-only (BM25)** — the
dense half is disabled when no embedding key is configured. Retrieval still
works; semantically-phrased questions (*"can I take my screen home?"* → monitor)
are the ones that degrade.

---

## Errors

Standard FastAPI shapes.

| Status | Meaning |
|---|---|
| `404` | Unknown asset code, employee, or route — including an unknown `to_employee` / `assign_to_employee` on a write |
| `409` | Replayed `idempotency_key` on `POST /transfers` |
| `422` | Request failed schema validation — e.g. `GET /recommendations` without the required `category`. Body carries the failing field |
| `429` | Input guardrail rate limit |
| `500` | Unhandled server error |

A filter that matches nothing is `200` with `[]` — an empty result is not an
error.

`/chat` is deliberately different: an **upstream provider failure does not
become an HTTP error**. It returns `200` with `outcome` set to `rate_limited` or
`timeout` and an explanatory `reply`, so a client always gets a well-formed
`ChatResponse`. Check `outcome`, not just the status code.
