# Setup Guide

A step-by-step guide to run the Asset Management Assistant on a fresh machine.
Takes about 5 minutes. Nothing here needs admin rights or a credit card.

> **The fastest way to see it working:** steps 1–4 run the full test suite and
> the REST API with **no API key at all**. A key is only needed for the live
> chat agent (step 5).

---

## Prerequisites

- **Python 3.10 or newer** — check with `python3 --version`.
  If you don't have it: [python.org/downloads](https://www.python.org/downloads/)
  (macOS also: `brew install python`).
- **git** — to clone the repository.
- macOS or Linux is assumed below. **Windows users:** see the note at the end.

---

## 1. Clone the repository

```bash
git clone https://github.com/Rahulkoi/asset-management-assistant.git
cd asset-management-assistant
```

## 2. Install

```bash
make install
```

This creates a local `.venv` and installs everything into it. Nothing is
installed system-wide.

## 3. Build the sample database

```bash
make seed
```

This reads the supplied spreadsheet and builds `data/assets.db` (35 assets, 13
employees) plus the policy search index.

## 4. Run the tests — proves the engineering, no key needed

```bash
make test
```

Expected: **`151 passed`** in about two seconds. These are deterministic — no
network, no API key. If this passes, the whole system minus the language model
is verified on your machine.

You can also explore the REST API with no key:

```bash
make run-api
```

Then open <http://localhost:8000/docs> for interactive API documentation, or:

```bash
curl localhost:8000/assets/AST1002
curl "localhost:8000/recommendations?category=Laptop&location=Bangalore"
```

---

## 5. Run the live chat agent (needs one free key)

The agent needs a language-model key. **Groq is recommended** — it's free, fast,
and takes about two minutes with no credit card.

**a.** Get a key at **<https://console.groq.com/keys>** (sign in with Google →
"Create API Key" → copy it).

**b.** Create your `.env` file:

```bash
cp .env.example .env
```

**c.** Open `.env` and paste your key after `OPENAI_COMPAT_API_KEY=`:

```
OPENAI_COMPAT_API_KEY=gsk_your_key_here
```

That's the only line you need to change.

**d.** Start the chat UI:

```bash
make run-ui
```

It opens at **<http://localhost:8501>**. Type a question, or click one of the
suggested prompts in the sidebar.

---

## What to try

| Ask this | What it shows |
|---|---|
| `Show details of AST1002` | Direct lookup by asset code |
| `How many printers do we have in Mumbai?` | Natural-language query → SQL filter |
| `Who is using AST1002, and who is that employee's manager?` | Multi-step reasoning |
| `Where is AST1002?` then `Who is using it?` | Conversation memory ("it" resolves) |
| `Find an available laptop in Bangalore` | Recommendation (excludes in-repair stock) |
| `What is the laptop refresh cycle?` | Policy search with a citation |
| `Transfer AST1002 to Priya Singh` → **Confirm** | The two-phase write gate — it previews, then commits only on your approval |

Expand the **trace panel** under any answer to see which tool the agent chose —
that's the "agentic" part: nothing is hardcoded to a phrasing.

To reset the data between runs: **Reset conversation** in the sidebar, or
`make seed` again.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make: command not found` | See the Windows note below, or run the raw commands in the Makefile. |
| Chat says "could not reach the language model" | The `OPENAI_COMPAT_API_KEY` in `.env` is missing or wrong. |
| Chat answer is a `404` about the model | Your Groq key may not serve `openai/gpt-oss-120b`. Run `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $YOUR_KEY"` to list models, and set `OPENAI_COMPAT_MODEL` in `.env` to one of them (e.g. `llama-3.3-70b-versatile`). |
| "Rate limited" after several quick questions | Groq's free tier is 8,000 tokens/minute. Wait ~40 seconds and continue. |
| Port 8501 or 8000 already in use | Stop the other process, or change the port: `make run-ui` uses 8501, `make run-api` uses 8000. |
| Sidebar shows "Policy index: BM25 only" | Expected — semantic embeddings are optional (see `.env.example`). Lexical search still works. |

---

## Windows

`make` isn't standard on Windows. Run these instead (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m assistant.db.seed          # seed
.venv\Scripts\python -m pytest -q                  # test
copy .env.example .env                              # then edit .env, add your key
.venv\Scripts\python -m streamlit run src\assistant\ui\streamlit_app.py --server.port 8501
```

---

## Where to read more

- **[README.md](README.md)** — what it does and the design decisions.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — component diagram and the guardrail table.
- **[docs/API.md](docs/API.md)** — full REST API reference.
