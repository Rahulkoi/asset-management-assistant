# 7-minute voiceover script (word-for-word)

Screen recording + voice, camera off. Read the **SAY** lines out loud; do the
**SCREEN** lines with your mouse/keyboard. It's a recording, so if any answer
rate-limits, just re-take that one clip.

**Before you hit record**
- `make seed` (resets data so the transfer demo works), then `make run-ui`
- Open <http://localhost:8501>, sidebar visible, conversation empty
- Have a second terminal ready for the `make test` moment near the end
- **Pace ~40s between questions** — the narration between them covers it
- Speak a touch slower than feels natural; pauses are fine

Total ≈ 7:00. Times are cumulative.

---

## 0:00 – 0:35 · What it is

**SCREEN:** UI open, sidebar showing asset count and model.

**SAY:**
> "Hi — this is an asset management assistant I built for an IT asset register.
> In plain terms: you ask it questions in English about company laptops, who has
> them, and IT policy — and it can also make changes, like transferring an asset
> from one person to another. But it never changes anything without showing you
> the change first and getting a yes.
>
> The thing I want to get across up front is this: the interesting part isn't the
> chatbot. It's that this agent can safely touch a real database. So most of the
> engineering here is the safety layer around the model, not the model itself.
> I'll show you what I mean."

---

## 0:35 – 1:00 · The data

**SCREEN:** Point at the sidebar — "Assets tracked 35", model, provider.

**SAY:**
> "Quick context on the data. The assignment came with a spreadsheet of 21
> assets. But two of the requirements — an employee's manager, and finding an
> *available* laptop — can't be answered from those columns; there's no manager
> field and no status field. So I added an employee table and a status column,
> and everything I generated is marked as synthetic in the database and listed in
> the README. The original 21 rows are stored unchanged. I didn't want to hide
> where the data came from."

---

## 1:00 – 1:35 · Requirement 1 & 2 — lookup and natural language

**SCREEN:** Type **`Show details of AST1002`** → send. When it answers, expand
the trace panel.

**SAY:**
> "Let's start simple — look up an asset by its code.
> *(answer appears)*
> There's the asset. Now watch this panel underneath — that's the trace. It
> shows the agent called one tool, `lookup_asset`. And the answer only contains
> what that tool returned."

**SCREEN:** Type **`How many printers do we have in Mumbai?`** → send. Expand trace.

**SAY:**
> "Now a totally different question, in plain English.
> *(answer appears)*
> Different question shape, so the agent picked a *different* tool —
> `search_assets`, with filters. And this is the key point about it being an
> agent: I never wrote 'if the user says how many, run a count.' The model reads
> the question and chooses the tool itself. Nothing is hardcoded to a phrasing."

---

## 1:35 – 2:10 · Requirement 3 — multi-step

**SCREEN:** Type **`Who is using AST1002, and who is that employee's manager?`**
→ send.

**SAY:**
> "This one needs two facts at once — who holds the asset, and who *their*
> manager is.
> *(answer appears)*
> Amit Kumar holds it, and his manager is Priya Singh. The manager data isn't in
> the original spreadsheet — it's that employee table I added. And I designed the
> lookup tool to return the holder *and* their manager together in one call, so a
> small model doesn't have to reliably chain three separate calls. That's a
> deliberate choice to make the hard requirement work on a free-tier model."

---

## 2:10 – 2:50 · Requirement 4 — conversation memory

**SCREEN:** Type **`Where is AST1002?`** → send. Then type **`Who is using it?`**
→ send.

**SAY:**
> "Now memory. First — where is this asset?
> *(answer: Bangalore)*
> And now I'll say 'who is using **it**' — without repeating the code.
> *(answer resolves to Amit Kumar)*
> It resolved 'it' correctly. And that's not a text trick looking for pronouns —
> the runtime records what was actually looked up in the previous turn and passes
> it back as context, so the model resolves the reference itself. That means it
> generalizes to phrasings I never anticipated, not just the word 'it'."

---

## 2:50 – 3:20 · Requirement 5 — recommendation

**SCREEN:** Type **`Find an available laptop in Bangalore`** → send.

**SAY:**
> "Last of the core requirements — recommend an available laptop.
> *(answer appears)*
> It lists available laptops, closest to Bangalore first. Here's the subtle part:
> there's actually a broken laptop — in repair — sitting in Bangalore too, and
> it's correctly *not* in this list. That filter lives in SQL, not in the prompt,
> because whether something is available is a hard fact — it shouldn't be left to
> the model's judgment."

---

## 3:20 – 4:05 · Policy questions — RAG, and knowing when to stop

**SCREEN:** Type **`What is the laptop refresh cycle?`** → send. Point at the
citation in the trace.

**SAY:**
> "Now a different kind of question — about policy, not assets. Asset questions
> are exact facts, so those run as SQL. But policy is written in documents, so
> those go through search over seven policy files.
> *(answer appears with citation)*
> It answers and cites the source document.
> But here's the part I care about most —"

**SCREEN:** Type **`How many annual leave days do I get?`** → send.

**SAY:**
> "— I'll ask something the policy doesn't cover.
> *(it declines)*
> It refuses. It doesn't guess. There's a relevance floor in front of the search,
> so when nothing in the documents is a good enough match, it returns nothing and
> says so, instead of citing the least-wrong paragraph. Knowing when to say
> nothing is the hard part of retrieval, and it's where these systems usually
> hallucinate."

---

## 4:05 – 5:25 · The write gate — the centerpiece

**SCREEN:** Type **`Transfer AST1002 to Priya Singh`** → send. Preview card
appears with Confirm / Cancel.

**SAY:**
> "Okay — the most important part. So far everything's been read-only. Now I'll
> ask it to actually *change* the database — transfer this asset to Priya.
> *(preview appears)*
> And notice — it did not do it. It shows me a preview of exactly what would
> change, and it says, right here, 'nothing has been changed yet.' It's waiting
> for me.
>
> First, let me *refuse*."

**SCREEN:** Click **Cancel**.

**SAY:**
> "I'll click Cancel.
> *(it cancels)*
> Nothing changed — the asset is still with Amit. And this was actually a real
> bug I found and fixed. The safety design is that a write needs approval on a
> later turn. But originally, *any* next message counted as approval — which meant
> saying 'no, cancel' accidentally approved the very transfer it was refusing.
> The Cancel button was arming the change it was supposed to stop.
>
> The fix was to actually read the user's answer, and default to no. Only a clear
> 'yes' goes through. Let me show you the yes path."

**SCREEN:** Type **`Transfer AST1002 to Priya Singh`** → send → preview appears →
click **Confirm**.

**SAY:**
> "Same request again — preview again, still nothing changed — and this time I
> confirm.
> *(it commits)*
> Now it's committed. Amit to Priya, with an audit record.
>
> The important idea: the model never decides whether to commit. The token that
> authorizes the write is inert until my next message releases it, and the user's
> words — in code — decide whether it's released. So an unapproved write isn't
> just discouraged, it's structurally impossible."

---

## 5:25 – 6:05 · The safety architecture

**SCREEN:** Optional — open ARCHITECTURE.md or the guardrail table.

**SAY:**
> "Stepping back — that write gate is one of about sixteen guardrails, across
> five stages. Before the model runs, there's rate limiting and an
> injection scan. The tools only accept validated arguments, and only run
> parameterized SQL — there's no free-form SQL the model could inject into. The
> loop is bounded — it can't run forever. And after the model answers, every
> asset code in the reply is checked against what the tools actually returned, so
> it can't confidently invent an asset that doesn't exist. The model always runs
> in the middle, with a guardrail on each side."

---

## 6:05 – 6:45 · Proof and the engineering story

**SCREEN:** Switch to the terminal, run **`make test`**. Show `151 passed`.

**SAY:**
> "And all of this is tested. This is the full suite — a hundred and fifty-one
> tests, no network, no API key needed. They run in about a second.
> *(151 passed shows)*
> One thing I'll call out, because it's the part I'm most proud of: that Cancel
> bug I mentioned had a test, and the test was *passing* — but for the wrong
> reason. It checked that the database didn't change, and it didn't — but only
> because the model happened to behave, not because the guardrail stopped it. A
> green test that measures the model's manners instead of the actual mechanism is
> worse than no test. So I rewrote it to assert the guardrail itself fired."

---

## 6:45 – 7:00 · Close

**SAY:**
> "To be clear about scope — this is a single-agent system with a real safety
> model. It's not multi-tenant, and there's no login yet. For production I'd add
> real identity, a per-user filter on every query so people only see their own
> assets, and durable state so it runs across multiple workers. But what's here
> today runs, it's tested, and it's safe to point at a real database. Thanks for
> watching."

---

### If you want to trim to fit

Cut in this order, they hurt least: the safety-architecture section (6:05), then
the data/provenance section (0:35). Never cut the write gate or the bug story —
those are what make it memorable.
