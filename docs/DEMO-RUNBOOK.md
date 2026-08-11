# Demo runbook

Operational companion to [`demo-script.md`](demo-script.md). That file is what
you *say*; this one is what can go wrong and how you answer for it.

---

## 1. The one constraint that will bite you

**Groq free tier gives you 8,000 tokens per minute. One agent turn costs
~4,000–5,000 tokens.**

That is **roughly one question per 40 seconds.** Two questions typed back to
back will rate-limit you, and the reply becomes
`Rate limited by https://api.groq.com/openai/v1`. This is measured, not
theoretical — it happened during pre-flight.

Practical rules:

- **Narrate between questions.** Your script already has 20–30 seconds of
  commentary per step. Deliver it *before* hitting enter on the next one, not
  after. Paced that way the whole demo fits inside the budget.
- **Never re-run a question because the answer looked slow.** The turn is
  ~1.5 seconds; if it's slow, it's the network, and a retry doubles your spend.
- **The `yes` confirmation turn is a full turn.** The transfer sequence costs
  two turns, ~9,000 tokens. Leave a clear gap before and after it.

If you get rate-limited on camera: say *"that's the free-tier token ceiling —
the runtime catches it, degrades to a clear message instead of a stack trace,
and the eval set is what quantifies model behaviour separately."* Then wait
~60 seconds and continue. It recovers on its own.

Check your headroom any time:

```bash
curl -s -D- -o /dev/null -X POST "$OPENAI_COMPAT_BASE_URL/chat/completions" -H "Authorization: Bearer $OPENAI_COMPAT_API_KEY" -H 'content-type: application/json' -d "{\"model\":\"$OPENAI_COMPAT_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" | grep -i ratelimit
```

---

## 2. Pre-flight, 15 minutes before

```bash
cd asset-assistant
make seed          # MUST run — resets the database to a known state
make verify-data   # expect: OK — all spreadsheet-sourced rows match
make test          # expect: 135 passed in ~1.5s
```

`make seed` is not optional. Any transfer you rehearse changes the database, and
a rehearsed demo leaves AST1002 already assigned to Priya Singh — which makes
your transfer step a no-op on camera.

**Port 8000 is currently occupied on your machine** by an unrelated project
("Aster & Oak Campaign Intelligence API"). Free it first, or the API section of
the demo will show the wrong app:

```bash
lsof -ti:8000 | xargs kill
```

Then start both surfaces, in separate terminal tabs:

```bash
make run-api    # :8000  → /docs
make run-ui     # :8501  → chat
```

Confirm the stack is actually wired up before you present:

```bash
curl -s localhost:8000/healthz
```

Expect `"database": true`, `"policy_index": true`, `"llm_configured": true`.

### Known state, so nothing surprises you

| | |
|---|---|
| Assets | 35 — 21 from the spreadsheet (`source=xlsx`), 14 added by seed (`source=synthetic`) |
| Status split | 21 Assigned · 12 Available · 2 In Repair |
| Employees | 13, three-level reporting line |
| In-repair units | AST1033 (HP EliteBook 840, Bangalore) · AST1034 (HP LaserJet Pro, Kolkata) |
| Policy corpus | 7 documents, 46 chunks |
| `embeddings` | **`false`** — see below |

**`embeddings: false` is expected right now, and you should be ready for it.**
Your `GEMINI_API_KEY` returns 403 — it's dead or the project has no billing
enabled. Retrieval is running lexical-only (BM25). It works: policy questions
that share vocabulary with the documents retrieve correctly, and the
out-of-scope refusal still fires. What degrades is purely-semantic phrasing —
*"can I take my screen home?"* won't reach the monitor policy.

Two options:

- **Leave it.** Stick to the policy questions in the script, which are lexically
  well-matched. If asked, answer honestly: *"the dense half needs an embedding
  key that isn't live on this machine; the retriever is designed to degrade to
  lexical rather than fail, and `/healthz` reports which mode it's in."* That is
  a good answer — it shows the degradation was designed, not accidental.
- **Fix it in 2 minutes** if you want hybrid on: get a fresh key at
  <https://aistudio.google.com/apikey>, put it in `.env`, `make seed` to rebuild
  the index, and `/healthz` should flip to `"embeddings": true`.

Do **not** attempt the fix in the last 20 minutes before the demo.

---

## 3. Mapping to what they actually asked for

Every line of the brief, and where you show it. Say the requirement number out
loud as you go — it makes the mapping impossible to miss.

| Brief requirement | Your demo step | Prompt |
|---|---|---|
| 1. Search by Asset Code | 0:20 | `Show details of AST1002` |
| 2. Natural language questions | 0:20 | `How many printers do we have in Mumbai?` |
| 3. Multi-step questions | 1:00 | `Who is using AST1002, and who is that employee's manager?` |
| 4. Conversation context | 1:30 | `Where is AST1002?` → `Who is using it?` |
| 5. Recommend assets | 2:00 | `Find an available laptop in Bangalore` |
| **Agentic (mandatory)** | throughout | Open the trace panel every time — it names the tool chosen |
| Add / transfer asset | 3:00 | `Transfer AST1002 to Priya Singh` → `yes` |

The mandatory requirement is *"the agent decides which tool to use, without
hardcoding every scenario."* **The trace panel is your evidence.** Expand it on
at least three different questions and point out that a different tool was
selected each time from the same code path. If you show nothing else, show that.

---

## 4. Deliverables checklist

| Deliverable | Status |
|---|---|
| Source code (GitHub) | ✅ pushed |
| README with setup | ✅ [`README.md`](../README.md) |
| Sample asset database | ✅ `data/Sample_Asset_Master.xlsx` tracked; `make seed` builds the SQLite DB |
| Architecture diagram | ✅ [`ARCHITECTURE.md`](../ARCHITECTURE.md) — Mermaid, renders inline on GitHub |
| API documentation | ✅ [`docs/API.md`](API.md) + live OpenAPI at `/docs` |
| 3–5 min demo video | ⬜ **record from [`demo-script.md`](demo-script.md)** |

On the video: **record it rather than presenting it live if you can.** It is a
deliverable in its own right, a retake costs nothing, and the token ceiling is
far less dangerous when you can pause between takes. Keep it under 5 minutes.

---

## 5. Questions they are likely to ask

Ordered by how likely you are to get them.

**"Is this actually agentic, or a router with if-statements?"**
The tool catalogue is passed to the model as function schemas; selection is the
model's, not a keyword match. Evidence: the trace panel shows a different tool
per question shape, and the eval set scores *tool selection* as its own
dimension across 45 cases. There is no phrase-to-tool mapping in the codebase —
point at [`tools/catalog.py`](../src/assistant/tools/catalog.py).

**"Why is there RAG at all? / Why not RAG over the assets?"**
Because 21 rows of clean structured data is not a retrieval problem. *"How many
printers in Mumbai"* needs an exact filter and a count; embedding a table you
can already query exactly trades correctness for nothing, and you end up with a
system that cannot count. Policy prose is the genuine retrieval problem, so the
system runs both and the model picks. This is a strong answer — lead with it.

**"How do you stop it writing to the database by mistake?"**
A write tool called without a confirmation token changes nothing. The token is
server-side, single-use, 5-minute TTL, bound to the session *and* a hash of the
arguments, and inert until a **later user turn** releases it. So committing
always requires a second human message. Mention that the model self-confirming
in one turn was a real bug you found and closed —
`test_model_cannot_confirm_its_own_write_in_one_turn` holds that line. Admitting
a caught bug reads as rigour, not weakness.

**"Where did the manager / availability data come from? It's not in the sheet."**
Answer this *before* they ask — it's in your script at 1:00. Two of the five
requirements are unanswerable from the supplied six columns. Everything added is
marked `source='synthetic'`, listed in the README provenance table, and the 21
supplied rows are untouched — `make verify-data` asserts that field by field.
Run it if they want proof.

**"What happens when the model hallucinates an asset code?"**
Every asset code in a reply is cross-checked against what the tools actually
returned. Unsupported code → one regeneration; if it survives, the answer ships
with a visible warning rather than passing an invention off as fact. There's an
eval case for it (`adv-hallucination-bait`).

**"Your eval report only shows 12 cases, not 45."**
Deliberate — a case costs ~4–5k tokens and the free tier gives 8k a minute, so
the committed run is a 12-case subset covering all eight categories. `make eval`
runs the full 45. Say the number honestly rather than implying the whole set ran.

**"Did you find anything interesting building this?"** — have this ready, it is
your best answer. Three real bugs, all caught by tooling rather than by luck:

1. **The model could confirm its own write.** It previewed a transfer and
   redeemed its own token in the same turn, reporting the change as done before
   the user saw anything — on roughly half of attempts. Fixed by making tokens
   inert until a later user turn releases them.
2. **The retrieval relevance floor accepted everything.** It was a fraction of
   the best BM25 score, and the best hit is 1.0 by construction, so "what is the
   capital of France" passed. Fixed with IDF-weighted term coverage.
3. **The eval scorer was scoring typography, not answers.** `gpt-oss-120b`
   emits U+202F — a narrow no-break space — inside proper nouns, so a reply
   reading "Amit Kumar" on screen is really `Amit Kumar`. A raw substring
   check failed it. Four correct cases were reported as failures until the
   scorer normalised Unicode before matching. **The lesson is the useful part:
   a red eval is not automatically the model's fault, and an eval you don't
   check can defame your own system.**

**"How do you know it works?"** — two separate things, and say so:
`make test` is 135 deterministic tests, no network, ~1.5s — a failure means the
*harness* is broken. `make eval` is golden cases against the real model, scored
on tool selection, content and guardrails separately, each against a fresh
database copy so `expect_db_unchanged` is a real assertion.

**"Why this model / what if the key runs out?"**
The runtime imports no vendor SDK — it talks to an `LLMClient` interface, so
Gemini or any OpenAI-compatible endpoint is a config change. Built that way
deliberately because free tiers run out. You are living proof: chat is on Groq
because the Gemini key died.

**"Is it production ready?"**
No, and say so plainly — the README lists the gaps. No authentication, every
request trusted as `actor`. Sessions and confirmation tokens are in-memory, so
multi-worker needs Redis behind the same two narrow interfaces. And
`asset_transfers` records who and what, but not the approval chain that policy
requires for cross-department transfers. Naming your own limits unprompted is
the strongest move available to you.

### Two sharp ones — have these ready

**"Your `/chat` transfer requires confirmation but `POST /transfers` commits
immediately. Isn't that a hole?"**
Deliberate asymmetry. An explicit API call *is* the confirmation — the caller
typed the asset code and the target. The two-phase gate exists for the path
where intent was *inferred* from natural language. Documented in
[`API.md`](API.md#the-two-phase-write-flow).

**"`/recommendations` excludes in-repair assets, but `POST /transfers` lets me
transfer one. Inconsistent?"**
Real gap, and the honest answer is better than a defence: REST is the raw
capability layer and the eligibility rule currently lives in policy, which only
the agent path consults. Enforcing it in the repository layer so both paths
inherit it is the right fix and it isn't done. Offer it as the next thing you'd
build.

---

## 6. If it goes wrong live

| Symptom | What to do |
|---|---|
| `Rate limited by …` | Name it as the free-tier ceiling, note it degrades cleanly, wait ~60s, carry on |
| Model gives an odd answer | Don't retry — say what the eval set measures and move on. Honesty costs less than a second failed attempt |
| UI won't start | Fall back to `curl` against the API — the script's questions all work as `POST /chat` |
| Everything is broken | `make test` — 135 tests, no network, always works. Close on it |

**`make test` is your reliable closer.** It needs no API key, no network, and
finishes in under two seconds. If the live model embarrasses you, this is the
thing that shows the engineering underneath is sound.
