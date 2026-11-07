# Veltrix Copilot

The internal assistant of **Veltrix Systems** (a fictional company). It answers questions from Veltrix's
650 company documents, with sources, and plans real business trips under the Veltrix travel policy
(POL-FIN-001 v2.0): live flight and hotel prices (Google Flights / Hotels via SerpAPI), email drafts to your
manager, and the trip in your Google Calendar.

Everything runs on your machine: the language model (Ollama), the database (Postgres + pgvector), the API
(FastAPI), the web app (Streamlit), the orchestrator (LangGraph) and the three MCP tool servers.
There is no simulated data: trip planning needs a SerpAPI key, and calendar features need Google Calendar.
Document questions and email drafts work without either.

## What you need

| | Why | Cost |
|---|---|---|
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | installs Python 3.12 and the exact package versions in `uv.lock` | free |
| Ollama with `qwen3:4b-instruct-2507-q4_K_M` and `nomic-embed-text` | answers, itineraries, email wording, embeddings | free |
| Docker (for Postgres + pgvector, on port 5433) | documents, trips, chats | free |
| A SerpAPI key | live flights and hotels | free plan: 250 searches/month |
| A Google account + a Google Cloud OAuth client | the calendar | free |

## Run it

```powershell
uv sync                                      # creates .venv with Python 3.12 and every package from uv.lock
copy .env.example .env                       # macOS/Linux: cp .env.example .env
# edit .env: SERPAPI_API_KEY, MANAGER_EMAIL, HOME_CITY, GOOGLE_CALENDAR_ID

ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama pull nomic-embed-text
docker compose up -d db
uv run -m veltrix_copilot.knowledge.ingest  # loads data/docs into pgvector (first run: see below)
uv run scripts/google_auth.py                # once (see "Google Calendar" below)
uv run scripts/dev.py                        # API, web app and the three MCP servers
```

Open http://127.0.0.1:8501 (API docs: http://127.0.0.1:8000/docs). To run only the web app:
`uv run streamlit run ui/app.py`. `uv run` always uses the project's `.venv`, so there's nothing to activate.
Try:

1. What is the hotel cap in Singapore?
2. How many incidents happened in 2025?
3. Give me a report of the company's performance in the first quarter of 2025.
4. Plan a 3-day trip from Kochi to Berlin next month with a budget of $2500. (Then answer **Yes** to save it
   and add it to Google Calendar, or **No** to discard it.)
5. Create a draft email to my manager with the travel plan.

**First ingestion.** The corpus is about 3,300 passages. Embedding them with `nomic-embed-text` takes a few
minutes on a GPU and noticeably longer on CPU; progress is printed as `[i/650]`. Ingestion re-embeds every file, so re-run it only
after changing documents outside the app (uploads on the Knowledge page are indexed on their own).

## The web app

A top bar switches between six pages (`ui/app.py`):

| Page | What it does |
|---|---|
| Chat | the conversation; recent chats in the sidebar; a workspace panel (stays in view as you scroll) with the latest trip, draft and event; Yes/No buttons when the assistant asks |
| Trips | every planned trip; change dates, nights, budget, flights or hotel, then apply |
| Calendar | your Google Calendar, with the trips the assistant added |
| Knowledge | the 650 documents by folder: search, upload, remove; older versions are marked |
| Outbox | the email drafts to your manager (never sent) |
| Status | which services are running (the icon turns red if one is down), light/dark theme (the browser remembers it; until you pick one it follows your computer), reset |

## The documents

`data/docs/` holds the Veltrix corpus in 17 folders (policies, projects, products, employees, offices,
incidents, meeting notes, …). Answers cite the file and passage, e.g. `[policies/pol-fin-001-travel-policy.md#3]`.
You can add PDF, Word, Markdown or text files on the Knowledge page; they go to `data/docs/uploads/`.

**Older versions.** A document counts as an older version only when it says so: it sits in an `archive`
folder, or its name says superseded/deprecated/old, or another file has a higher `vN`. Today that is just
`policies/archive/pol-fin-001-travel-policy-v1-3-superseded.md`. It stays searchable, but is used only when a
question asks for it ("the previous travel policy", "v1.3").

**Questions search can't answer alone.** Search returns the few passages most like the question, which isn't
enough for some questions, so `veltrix_copilot/knowledge/lookup.py` finds these documents in code first:

- *Counting and listing* ("How many incidents happened in 2025?", "Which customers are in Germany?"): the
  collection's index table (`incidents/index.md`, `customers/index.md`, `projects/portfolio.md`, …, or a list of
  the folder's files) is read whole, filtered to the year and values named, and counted in code.
- *Quarters* ("the first quarter of 2025", "Q3 FY2026"): the matching `finance/quarterly-reports/fyYYYY-qN.md`.
  Veltrix's fiscal year runs April – March, so FY2025 Q1 is April – June 2024; the answer says so and how to ask
  for calendar January – March 2025 instead (FY2025 Q4).
- *Documents named by ID or file name* ("INC-2025-0042", "POL-FIN-001", "fy2025-q1.md").

**Measuring retrieval.** `evals/veltrix_qa.jsonl` has 488 questions with their source files
(`evals/CORPUS_README.md` describes the corpus):

```
uv run scripts/eval_retrieval.py                       # top-k hit-rate over all questions
uv run scripts/eval_retrieval.py --k 8                 # with more passages
uv run scripts/eval_retrieval.py --limit 50 --answers  # also generate answers and check them (slow)
```

## The travel policy in the trip planner

`veltrix_copilot/travel/travel_rules.py` holds the POL-FIN-001 v2.0 numbers; `tests/test_policy_consistency.py`
checks them against the policy document, so the two can't drift apart.

| Rule | Value |
|---|---|
| Hotel cap per night | ₹9,000 India Tier-1 · ₹6,000 rest of India · $220 US · €180 EU · S$260 Singapore |
| Per diem | ₹4,500 · ₹3,000 · $75 · €60 · S$90 (same regions) |
| Booking notice | 7 days domestic, 21 days international |
| Cabin | economy; premium economy on international flights over 6 hours; business needs CFO approval |
| Approval | department head above $5,000 |

Two assumptions, because the policy doesn't say:

- **Tier-1 cities** are Bangalore, Mumbai, Delhi, Chennai, Hyderabad, Kolkata, Pune and Ahmedabad
  (`INDIA_TIER1`). Change the set if Finance uses a different list.
- **Other regions** (e.g. the United Kingdom, the UAE, Japan) have no limits in the policy. The planner then
  uses the US amounts and says the region isn't covered, so check with Finance.

## Google Calendar

Create a Google Cloud project and enable the **Google Calendar API**. Set up an External OAuth consent screen in
Testing mode with yourself as a test user, create a **Desktop app** OAuth client, and save its JSON as
`secrets/google_client_secret.json`. Then run `uv run scripts/google_auth.py`. A dedicated calendar is
recommended: create "Veltrix Copilot" in Google Calendar and put its ID in `GOOGLE_CALENDAR_ID`.
In Testing mode the sign-in expires after 7 days; run the script again. `uv run scripts/google_auth.py --revoke`
disconnects it.

**Trips and the calendar.** A planned trip is shown first and saved only when you answer **Yes**; it is then
added to Google Calendar too (if it overlaps other events, you're asked first). *Save this trip* works later
in the same chat, and *plan a trip to … and add it to my calendar* saves it straight away.

**If the Calendar page says you're not signed in** after running the script:
`uv run scripts/google_auth.py --status` shows where it looks for the sign-in (`secrets/google_token.json` in this
project folder) and tests it against Google. The app picks up a new sign-in by itself, without a restart.

## Live prices and their limits

Every new trip uses about 3 SerpAPI searches (outbound, return, hotel), more if the return has to move by a
day; *show me other flights* uses 2. Identical searches are answered from a 24-hour local cache for free.
The app stops itself at `SERPAPI_MONTHLY_LIMIT` (default 100) or when SerpAPI has fewer than
`SERPAPI_MIN_SEARCHES_LEFT` left, and then says so instead of planning. Prices change; nothing is booked.

## How a request flows

`ui/app.py` → `POST /chat` (`veltrix_copilot/api/main.py`) → LangGraph (`veltrix_copilot/orchestrator/graph.py`):
the rule-based **router** picks agents (`rag`, `travel`, `email`, `calendar`, `trips`), each agent runs in
turn, and **merge** joins their answers. Agents use tools only through MCP (`veltrix_copilot/tools/mcp_client.py`):
`mcp_servers/travel_server.py` (8101, SerpAPI), `email_server.py` (8102, drafts only, never sent) and
`calendar_server.py` (8103, Google Calendar). All model calls go through `veltrix_copilot/core/llm.py`.

```
veltrix_copilot/
  core/          config, model calls (llm), schemas, dates, currency, small helpers
  knowledge/     database, document loaders, ingestion, older-version rules
  travel/        policy numbers (travel_rules), policy checks, trips store, trip editing
  agents/        the five agents, plus plans.py (which trip a follow-up means)
  orchestrator/  the graph, router and state
  tools/         the MCP client
  api/           FastAPI app and chat history
mcp_servers/     the tool servers, the SerpAPI and Google clients, data/cities.json
ui/              app.py (entry point), views/ (one file per page), components/ (API client, nav, prefs), assets/
data/docs/       the Veltrix documents
evals/           the 488-question set and the corpus notes
pyproject.toml   dependencies (uv); uv.lock pins every version; .python-version picks Python 3.12
scripts/         dev.py (start everything), google_auth.py, eval_retrieval.py
tests/           offline checks: uv run pytest
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| The Status icon in the top bar is red | open Status: it shows which service is down |
| "Live travel search failed: no SERPAPI_API_KEY set" | add the key to `.env`, restart `scripts/dev.py` |
| "monthly safety limit … reached" | wait for next month, or raise `SERPAPI_MONTHLY_LIMIT` |
| "Google Calendar is not connected" | `uv run scripts/google_auth.py` |
| Very slow answers | `ollama ps` must show 100% GPU; lower `NUM_CTX` or use a smaller model |
| `psycopg … connection timeout expired` / `Can't reach Postgres` | start Docker Desktop, run `docker compose up -d db`, wait for `docker compose ps` to say *healthy*; `DATABASE_URL` in `.env` must be `postgresql://app:app@127.0.0.1:5433/veltrix` |
| `ModuleNotFoundError` | `uv sync` (and start commands with `uv run`) |
| "No documents ingested yet" | `uv run -m veltrix_copilot.knowledge.ingest` |
