# Asset Management Assistant — XYZ Technologies

An agentic AI assistant over the IT asset register. It answers questions about
assets, people and policy, and can add or transfer assets — but never changes
anything without showing you the change first and getting a yes.

```
You: Who is using AST1002, and who is that employee's manager?
AI:  AST1002 (Lenovo ThinkPad X1, Bangalore) is assigned to Amit Kumar,
     whose manager is Priya Singh (Engineering).

You: Find an available laptop in Bangalore
AI:  Three are free in Bangalore:
       AST1022  MacBook Pro M3       New, purchased 2025-09-02
       AST1021  Dell Latitude 7440   New, purchased 2025-06-10
       AST1023  Lenovo ThinkPad X1   Good, purchased 2024-03-18

You: Transfer AST1002 to Priya Singh
AI:  This would move AST1002 (Lenovo ThinkPad X1) from Amit Kumar to
     Priya Singh (Engineering). Nothing has changed yet — shall I go ahead?
You: yes
AI:  Done. AST1002 is now assigned to Priya Singh.
```

---

## Quick start

```bash
make install                      # venv + dependencies
cp .env.example .env              # add GEMINI_API_KEY (free: aistudio.google.com/apikey)
make seed                         # build data/assets.db from the spreadsheet
make test                         # 138 tests, no API key needed
make run-ui                       # chat UI at localhost:8501
```

The REST API and the whole test suite work **without** an API key. Only the
agent itself needs one.

```bash
make run-api                      # API + OpenAPI docs at localhost:8000/docs
make eval                         # run the 45-case golden set
```

---

## Data provenance — read this first

The supplied spreadsheet has 21 rows and six columns: Asset Code, Asset Name,
Category, Employee Name, Location, Purchase Date. **Two of the five functional
requirements cannot be answered from it**, so the seed adds data. Everything
generated is marked `source='synthetic'` in the database and listed here.

| Requirement | What is missing | What was added |
|---|---|---|
| *"…and who is that employee's manager?"* | No manager, department or e-mail column | An `employees` table: 13 people from the spreadsheet, given a three-level reporting line, departments and `@xyztech.example` addresses |
| *"Find an **available** laptop in Bangalore"* | No status column; all 21 assets are assigned | A `status` column, plus 14 spare assets (AST1021–AST1034) — 12 available, 2 in repair |

The two in-repair units are deliberate: they give the recommendation tool
something it must correctly *refuse* to offer, which is asserted in both the
test suite and the eval set.

The 21 supplied rows are stored **unchanged**. `make verify-data` re-reads the
spreadsheet and asserts every one of them still matches, field by field:

```bash
$ make verify-data
OK — all spreadsheet-sourced rows match Sample_Asset_Master.xlsx
```

The org chart lives in one readable dict at the top of
[`src/assistant/db/seed.py`](src/assistant/db/seed.py) — declared as a literal
table, not generated, so a reviewer can check it at a glance.

---

## What it does

| # | Requirement | How |
|---|---|---|
| 1 | Search by asset code | `lookup_asset` — exact fetch, case-insensitive |
| 2 | Natural language questions | `search_assets` — filters on category, location, status, holder, name, purchase-date range |
| 3 | Multi-step questions | `lookup_asset` returns the holder *and* their manager in one call; `lookup_employee` walks the reporting line further |
| 4 | Conversation context | Referents recorded from tool results and replayed as session context, so "who is using **it**?" resolves |
| 5 | Recommendations | `recommend_assets` — available stock only, ranked exact-city → same-region → elsewhere, newest first |
| — | Policy questions | `search_policy` — hybrid RAG over seven policy documents, with citations |
| — | Add / transfer an asset | `add_asset`, `transfer_asset` — two-phase, preview then confirm |

The agent chooses which tool to use; nothing is hardcoded to a phrasing. The
seven tools, their descriptions and their selection rules are in
[`tools/catalog.py`](src/assistant/tools/catalog.py).

---

## Why RAG here, and not over the assets

**The asset table is not a RAG problem.** 21 rows of clean structured data, with
questions like "how many printers in Mumbai" that need exact filters and counts.
SQL answers those precisely and cheaply. Embedding a table you can already query
exactly trades correctness for nothing — it is the most common way RAG gets
misapplied, and it would have produced a system that cannot count.

**The policy corpus is a RAG problem.** The brief implies rules the spreadsheet
does not contain — eligibility, approvals, warranty, refresh cycles, loss
reporting, WFH equipment, offboarding. That is genuinely unstructured prose
where the answer is a passage, not a row. So the assistant runs both: SQL for
facts, retrieval for prose, and the model picks per question.

Retrieval is **hybrid** — BM25 plus `gemini-embedding-2` vectors (768
dimensions), fused with reciprocal rank fusion. Two shapes of question show
up and each breaks one retriever on its own:

- *"What does the AMC cover for printers?"* — the words are in the document;
  BM25 wins.
- *"Can I take my screen home?"* — nothing matches "monitor" lexically; the dense
  half wins.

RRF is used rather than a weighted score blend because BM25 scores are unbounded
and corpus-dependent while cosine sits in [-1, 1] — normalising them onto one
scale means tuning constants that only fit this corpus. RRF consumes ordering
only, so there is nothing to tune.

**Knowing when to say nothing** is the part that matters. A relevance floor sits
in front of the results: below it, `search_policy` returns *no* passages, and
the agent says the policy does not appear to cover the question rather than
citing the least-wrong paragraph.

The floor is IDF-weighted term coverage, not a fraction of the best BM25 score.
Normalising against the best hit is self-defeating — the best hit is 1.0 by
construction, so a relative floor accepts everything, including "what is the
capital of France". (That was a real bug caught by the test suite, not a
hypothetical.) Weighting by IDF also makes the floor discriminating: matching
"stolen" counts far more than matching "days", and a query term absent from the
corpus counts fully against the score.

**The hybrid half was dead code for most of this project's life**, and the way
it failed is the interesting part. `embed_texts` passed a `list[str]` to the
Gemini SDK, which looks like a batch but is read as *the parts of one
document*: 32 chunks went in, one vector came out. A length guard then noticed
the mismatch and correctly returned `None` rather than misaligning vectors
against chunks — so retrieval silently fell back to lexical, with a valid key,
a populated corpus, and no error anywhere. `/healthz` reported
`embeddings: false` and that was taken as a missing key rather than a bug.

Fixed by wrapping each text in its own `Content`. The guard stayed: degrading
to lexical is right, and silently-wrong retrieval would have been worse than
silently-absent retrieval. What was missing was any signal that the degrade had
happened — which is why `/healthz` now earns its place in the pre-flight check.

Measured on the current corpus with hybrid actually running: **7/7 recall on
in-scope questions, 5/6 correct refusals.** The cosine floor is 0.68, chosen
from the measured gap between the lowest in-scope score (0.698) and the highest
out-of-scope one (0.658, *"how many annual leave days do I get?"*). A 0.04
margin is thin, it is tuned to this corpus and this embedding model, and both
numbers should be re-measured if either changes.

The one remaining leak — *"what is our revenue this quarter?"* — passes the
**lexical** floor, not the semantic one, by sharing a rare token with an
unrelated section. It is documented in [`tests/test_rag.py`](tests/test_rag.py)
rather than hidden.

---

## Guardrails

Sixteen controls across five layers — the full table is in
[ARCHITECTURE.md](ARCHITECTURE.md#guardrail-layers). The important ones:

**Writes cannot happen without approval.** A write tool called without a
confirmation token changes nothing — it validates, builds a preview, and returns
a token. Only a second call carrying that token commits. The token is minted
server-side, single-use, expires in five minutes, and is bound to the session
*and* a hash of the arguments. So:

- it cannot reuse an approval for a second write;
- it cannot get approval for one change and commit a different one;
- an injected instruction cannot drive a write, however persuasive.

Binding is not sufficient on its own, and it is worth being precise about why.
The token is handed to the model inside the tool result, so "the model cannot
invent a token" is true but irrelevant — it does not need to invent one. Left
there, the model can preview a write and redeem its own token in the *same*
turn, reporting the change as done before the user has seen anything. That was
a real defect in this codebase, not a hypothetical: the live model did it on
roughly half of attempts, and the test suite missed it because it only covered
*forged* tokens and correctly-behaved next-turn commits.

So a token is inert until a later user turn releases it
(`ConfirmationStore.release_for_session`, called at the top of `run_turn`).
Committing therefore always requires a second user message.
`test_model_cannot_confirm_its_own_write_in_one_turn` holds that line.

**A turn boundary is a delay, not a veto** — and treating the two as the same
thing was the second defect here, subtler than the first. Releasing on *any*
next message meant the user's actual answer was never consulted: "no, cancel
that" approved the very write it refused, and the UI's own Cancel button armed
the change it existed to stop. What remained between a refusal and a commit was
the model choosing to be polite, which is precisely the discretion this design
exists to remove.

The eval case for cancellation passed the whole time. It asserted the database
was unchanged, and it was — because the model declined to redeem a token it had
been handed and was fully entitled to use. A green test measuring manners
rather than mechanism is worse than no test, because it buys confidence it has
not earned.

So the *content* of the answer decides, and the default is deny.
`interpret_confirmation` reads the user's reply in code: a clear affirmative
releases the preview, and anything else — a refusal, a question, a change of
subject — **destroys** it, so a preview the user never accepted cannot be
redeemed by a later turn. Discarding rather than leaving it unapproved matters,
since an unapproved token still sits in the conversation waiting for a second
chance. Three tests hold this: `test_refusal_destroys_the_pending_write`,
`test_changing_the_subject_destroys_the_pending_write`, and
`test_approval_still_commits`.

Parsing assent in code is not the same as asking the model nicely to respect
it — the model never sees this decision and cannot argue with it. The cost is
that an unusually phrased approval reads as "no decision" and the write must be
requested again. That is a safe failure, and the correct direction to fail in.

That is the design's centre of gravity. Prompt-level defences reduce the chance
of a mistake; a token that is turn-gated *and* consent-gated makes an
unapproved write structurally impossible.

**Grounded answers.** Every asset code in a reply is cross-checked against what
the tools actually returned. An unsupported code triggers one regeneration; if
it survives that, the answer ships with a visible warning rather than passing an
invention off as fact.

**Bounded work.** 8 tool calls, 6 iterations, 90 seconds per turn. On the final
pass the tools are taken away, forcing the model to answer with what it has
instead of stopping mid-plan.

**Untrusted content stays untrusted.** Retrieved passages are fenced and
labelled as data. Injection patterns in user input are flagged rather than
blocked — someone testing the assistant deserves an answer, and the real
protection is the token gate, which is unaffected either way.

---

## Testing and evaluation

Two separate things, because they fail for different reasons.

**`make test` — 138 tests, no network, ~0.9s.** Everything deterministic: the
data layer, tool dispatch and validation, all guardrails, the agent loop (driven
by a scripted fake model), retrieval, the API, and the eval scorer itself. A
failure here means the *harness* is broken.

**`make eval` — 45 golden cases against the real model.** Covers all five
functional requirements, policy retrieval, the write flow, and eight adversarial
cases. Each case runs against a fresh copy of the database, so
`expect_db_unchanged` is a real assertion rather than a hope. Scored on three
dimensions separately:

- **tool selection** — did it reach for the right capability?
- **content** — is the answer actually right? (exact substring checks on codes,
  names and dates — no LLM judge, so the eval adds no non-determinism or cost)
- **guardrails** — did the safety behaviour hold?

```bash
make eval-dry                     # validate cases without spending quota
make eval                         # full run → evals/report.md
./.venv/bin/python -m evals.runner --filter 7-writes --limit 5
```

**The runner paces itself, and has to.** A case costs ~4–5k tokens; the Groq
free tier allows 8k per minute. Run flat out and the suite rate-limits itself,
at which point the report measures the provider's quota rather than the agent —
so `--delay` defaults to 45 seconds between cases. Budget ~35 minutes for a full
45-case run, or drop it to `--delay 0` on a paid tier.

The scorer is itself tested: `tests/test_evals.py` feeds it known-good and
known-bad agent behaviour, including a deliberately misbehaving model that
self-confirms a transfer, to prove the unauthorised-write assertion actually
fires.

---

## API

Full reference with request/response shapes, error codes and the write flow:
**[`docs/API.md`](docs/API.md)**.

`make run-api`, then <http://localhost:8000/docs> for interactive OpenAPI docs.

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Ask the agent. Returns the reply, the tools it used, citations, guardrails that fired, token usage, and any pending confirmation. |
| `POST /chat/stream` | Same, as SSE — `tool_start` / `tool_end` events, then the result. |
| `GET /assets/{code}`, `GET /assets` | Look up or filter assets |
| `GET /employees/{name}` | Profile, manager, reports, assets held |
| `GET /recommendations` | Available stock only |
| `POST /assets`, `POST /transfers` | Deterministic writes |
| `GET /audit` | Every mutation, newest first |
| `GET /healthz` | Component status |

The deterministic endpoints are not filler — they are the tool layer's own
contract, which means the whole system minus the model can be tested and
integrated without an LLM. The model adds a natural-language interface over a
real service; it is not the service.

```bash
curl localhost:8000/assets/AST1002
curl "localhost:8000/recommendations?category=Laptop&location=Bangalore"
curl -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"session_id":"demo","message":"Who is using AST1002 and who is their manager?"}'
```

Note the asymmetry: `POST /transfers` commits immediately, while the same
operation through `/chat` requires confirmation. An explicit API call *is* the
confirmation; the two-phase gate exists for the path where intent was inferred
from natural language.

---

## Switching model provider

The runtime imports no vendor SDK — it talks to an `LLMClient` interface. Gemini
is primary; any OpenAI-compatible endpoint (Groq, OpenRouter, vLLM) is a config
change:

```bash
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_API_KEY=...
OPENAI_COMPAT_BASE_URL=https://api.groq.com/openai/v1
OPENAI_COMPAT_MODEL=llama-3.3-70b-versatile
```

Built this way deliberately: the app runs on a free tier, and free tiers run out.

---

## Project layout

```
src/assistant/
  llm/          provider abstraction (base · gemini · openai_compat)
  db/           schema.sql · seed.py · repo.py  ← only module importing sqlite3
  tools/        7 typed tools + registry + catalog
  rag/          chunker · index · hybrid retriever
  agent/        runtime (the loop) · prompts · memory · confirm
  guardrails/   input · injection · limits · output
  api/          FastAPI app
  ui/           Streamlit chat client
  obs/          structured JSONL tracing
docs/policies/  the RAG corpus (7 documents)
evals/          cases.yaml · runner.py · report.md
tests/          138 tests
```

Deeper design notes, the guardrail table and sequence diagrams are in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Known limitations

- **Single-process state.** Sessions and confirmation tokens live in memory.
  Multi-worker deployment needs Redis behind the same two interfaces
  (`SessionStore`, `ConfirmationStore`) — both are deliberately narrow.
- **Free-tier tool calling** on multi-step chains is less reliable than frontier
  models. Mitigated by returning the holder's manager with the asset so the
  multi-step question needs one hop, not three. The eval set quantifies the gap
  rather than hiding it.
- **Retrieval false positive** documented in `tests/test_rag.py`: a query
  sharing a rare token with an unrelated section can pass the lexical floor.
  The dense half separates them; the agent still has to read the passage and
  decide.
- **No authentication.** Every request is trusted as `actor`. Real deployment
  needs SSO, and per-user authorisation on the write tools.
- **`asset_transfers` records who and what, not the approval chain.** Policy
  requires manager approval for cross-department transfers; the system records
  the change but does not yet enforce or capture that approval.
