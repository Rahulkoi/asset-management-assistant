# How it works — understand it well enough to explain it

This is not a script. It's the mental model. Read it once and you'll be able to
talk through the whole system in your own words and answer follow-ups. For the
exact demo prompts and timings, see [demo-script.md](demo-script.md); for what
to do if something breaks on camera, see [DEMO-RUNBOOK.md](DEMO-RUNBOOK.md).

---

## 1. The one-sentence version

> "It's an AI assistant that sits in front of a company's IT asset database. You
> ask it questions in plain English about laptops, who has them, and company
> policy — and it can also move assets between people, but never without showing
> you the change and getting a yes."

If you can only say one more sentence, say this:

> "The interesting part isn't the chat — it's that it can touch a real database
> safely, so most of the engineering is the guardrails, not the model."

---

## 2. Why it's an "agent", not just a chatbot

A chatbot answers from what the model already knows. This doesn't. Every time
you ask something, the model is handed a set of **tools** — functions it's
allowed to call, like `lookup_asset`, `search_assets`, `transfer_asset` — and it
**decides which one to call** based on your question.

Nobody wrote "if the user says *how many*, run a count." The model reads the
question and picks the tool. That is the whole meaning of "agentic": the model
chooses the actions, the code just runs them safely.

**What you show for this:** the trace panel under each answer. Different question
shapes call different tools, from the same code. That panel is your proof.

---

## 3. The layers, bottom to top

Picture six layers. You built all of them.

| Layer | What it is | In the code |
|---|---|---|
| **Data** | A SQLite database (assets, employees, an audit log) + 7 policy documents | `db/`, `docs/policies/` |
| **Tools** | 7 functions that read or write that data, each with a strict input schema | `tools/` |
| **Agent runtime** | The loop that runs one turn: ask model → run tools → repeat → answer | `agent/runtime.py` |
| **Guardrails** | Safety checks wrapped around the model at every stage | `guardrails/`, `agent/confirm.py` |
| **Provider layer** | The actual LLM (Groq, Gemini, …), swappable behind one interface | `llm/` |
| **Interfaces** | A web chat UI and a REST API | `ui/`, `api/` |

---

## 4. One question, step by step (the core thing to understand)

Take **"Who is using AST1002, and who is that employee's manager?"** Here is
exactly what happens — this is the walkthrough to internalize.

1. **The question arrives** at the runtime (from the UI or `POST /chat`).
2. **Input guardrails run first**, before the model sees anything: is this
   session over its rate limit? Is the message suspiciously long? Does it look
   like a prompt-injection attempt ("ignore your instructions")? Injection gets
   *flagged*, not blocked — the real protection is later.
3. **Conversation context loads** — the history and a note of what was last
   looked up. (This is what lets "it" resolve in a follow-up question.)
4. **The model is called** with three things: a system prompt (its rules), the
   history, and the list of 7 tools.
5. **The model responds with a tool call:** `lookup_asset(asset_code="AST1002")`.
6. **The runtime validates that call** — is `lookup_asset` a real tool? Do the
   arguments match its schema? — then **runs it**, which is a parameterised SQL
   query. No free-form SQL ever touches the database.
7. **The tool returns the asset *and* the holder's manager together.** This is a
   deliberate design choice: it means the model gets both facts from one call
   instead of having to chain three calls, which small free-tier models do
   unreliably.
8. **The result goes back to the model**, which now writes the English answer.
9. **Output guardrails check the answer:** every asset code in the reply is
   cross-checked against what the tools actually returned, so the model cannot
   invent an asset that doesn't exist. If policy was used, a citation is required.
10. **The answer is returned**, and a full trace of the turn is written to a log.

The whole turn is **bounded**: at most 8 tool calls, 6 loops, 90 seconds. On the
last loop the tools are taken away, which forces the model to answer with what it
has instead of stopping half-way through a plan.

**Say it like this:** "The model only ever runs in the middle, inside a bounded
loop, with a guardrail on each side. It never talks to the database directly —
it asks for a tool, the code decides whether to run it."

---

## 5. The write gate — the centerpiece, explain this slowly

Reads are safe. **Writes** — transferring or adding an asset — are where an AI
touching a real database gets dangerous. You don't want it changing records
because it misread you, or because a malicious policy document told it to. So
every write is **two-phase**:

- **Phase 1 — you ask.** "Transfer AST1002 to Priya." The model calls
  `transfer_asset`. But the tool **does not do it.** It checks the request is
  valid, builds a **preview** ("this would move X from Amit to Priya"), mints a
  one-time **token**, and returns — **changing nothing.**
- **Phase 2 — you approve.** You see the preview and say "yes." *Now* the model
  can call `transfer_asset` again carrying that token, and it commits — one
  database update plus an audit row, in a single transaction.

The two rules that make this airtight:

1. **The token is inert until your *next* message releases it.** So the model
   physically cannot preview and commit in the same breath.
2. **Only a clear "yes" releases it. Anything else — "no", a question, a change
   of subject — destroys the preview.** So a change you refused cannot be
   committed later.

**The line that lands in an interview:** "This isn't the model being polite. The
model never decides whether to commit — the user's own words decide, in code. An
unapproved write is structurally impossible, not just discouraged."

**What you show:** transfer → preview appears, "Nothing has been changed yet" →
click **Cancel** → the database is untouched → then do it again and click
**Confirm** → now it commits, with an audit row.

---

## 6. Why there's *both* SQL and search (RAG)

Two kinds of question need two kinds of answer:

- **"How many printers in Mumbai?"** is a *fact*. There's an exact number.
  Embedding a 21-row table and doing fuzzy similarity search over it would be
  slower and could get the count wrong. So facts run as **SQL** — precise.
- **"Can I take my monitor home?"** is *prose*. The answer is a passage in a
  policy document, phrased differently from the question ("monitor" vs "screen").
  So policy runs as **search** over the 7 documents.

The search is **hybrid** — it finds passages two ways at once:
- **keyword match** (BM25) — good when the question uses the document's words,
- **meaning match** (embeddings) — good when it doesn't ("screen" → monitor).

It combines the two rankings, and then does the part that actually matters:
**a relevance floor.** If nothing clears the bar, it returns *nothing*, and the
assistant says "the policy doesn't appear to cover that" instead of citing the
least-wrong paragraph.

**Say it like this:** "Knowing when to say nothing is the hard part of RAG.
Anyone can return the top result; refusing to answer when the corpus doesn't
cover the question is what stops it hallucinating policy."

---

## 7. The provider layer — why it survived the free-tier limits

The runtime never imports a vendor's SDK. It talks to one interface
(`LLMClient`), so the actual model — Groq, Gemini, NVIDIA — is a one-line change
in `.env`. And because several providers share the same wire format, they
**chain**: if the primary one hits its rate limit mid-answer, the turn quietly
drops to the next instead of failing.

**Say it like this:** "A single free tier is a single point of failure. The
provider layer degrades through a chain instead of dying — so one provider
running out doesn't end a turn."

---

## 8. The engineering story — your strongest material

Interviewers remember the bugs, not the features. You found five, and four were
*invisible* — passing tests, no errors. Lead with the first one.

1. **"Cancel" armed the write it was meant to stop.** The write gate looked
   correct, and the eval case for cancellation *passed* — but only because the
   model chose not to redeem a token it was holding and fully entitled to use.
   The guardrail wasn't holding; the model was being polite. Fixed by reading the
   user's actual answer, default-deny. **The lesson: a green test that measures
   the model's manners instead of the mechanism is worse than no test.**
2. **The model could approve its own write in one turn** — closed by making the
   token need a later turn. (This is the bug that *created* the gap in #1.)
3. **Hybrid search never actually ran.** A formatting bug sent 32 document
   chunks to the embedding API as if they were one document — 32 in, 1 vector
   out — so it silently fell back to keyword-only, with a valid key and no error.
   Fixed, then the relevance threshold had to be re-measured against real scores.
4. **A scoring test failed for the wrong reason** — the model emitted a Unicode
   "narrow no-break space" inside names, so "Amit Kumar" didn't string-match
   "Amit Kumar". The answer was right; the scorer was wrong.

**The theme to name out loud:** "Two of these were a test passing for the wrong
reason and a test failing for the wrong reason. Both were found by distrusting a
green tick and a red cross. That habit is most of what the job is."

---

## 9. What you'd add for production (say this before they ask)

This is a **single-agent** system with a real safety model — not a multi-agent
platform, and you should say so plainly. What production hardening adds:

- **Identity & multi-tenancy** — real login, per-request user scope, nothing
  trusted by default.
- **A Policy Enforcement Point** — a row-level filter on *every* query, so a
  user only ever sees their own assets.
- **Durable state** — sessions and tokens in Redis instead of process memory, so
  it runs multi-worker.
- **Approval chains** — capturing the manager sign-off that cross-department
  transfers require.

**Say it like this:** "Here's what runs today, and here's exactly what I'd build
next and why. Claiming the roadmap already exists is the fastest way to get
caught — naming it is what shows judgment." See the diagrams and the
built-vs-production table in [ARCHITECTURE.md](../ARCHITECTURE.md).

---

## 10. Recording the video — a shape that works

Aim for 4–5 minutes. Structure:

1. **(20s) What it is** — the one-sentence version + "the engineering is the
   guardrails, not the model."
2. **(2 min) The five requirements, live** — lookup → natural language →
   multi-step → context ("it") → recommendation. Open the trace panel each time
   and point out a *different* tool was chosen. Use the exact prompts in
   demo-script.md.
3. **(30s) Policy RAG** — one in-scope question (gets a citation), one
   out-of-scope question (it refuses). The refusal is the point.
4. **(60s) The write gate** — transfer → Cancel (nothing changes) → transfer →
   Confirm (commits). This is your headline; slow down here.
5. **(30s) Proof + story** — run `make test` (151 pass, no key), then tell the
   "Cancel armed the write" bug story over the architecture diagram.
6. **(15s) Close** — "single-agent today; here's what production adds," name one
   or two roadmap items, done.

**Two rules while recording:**
- **Pace ~40 seconds between questions** so Groq's per-minute limit never hits.
- **Narrate the *why* while the answer loads** — that's where understanding
  shows, and it fills the wait naturally.

If you can explain sections 4 and 5 without reading, you're ready.
