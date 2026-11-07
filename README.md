# Veltrix Copilot

Veltrix Copilot is an internal assistant for **Veltrix Systems**, a made-up industrial software company with a
very real-looking pile of paperwork: 650 documents covering policies, projects, customers, incidents, finance
reports, meeting notes and more. All of them are AI-generated (more on that [below](#about-the-documents)).

You can ask it things like *"What's the hotel cap in Singapore?"* or *"How many incidents did we have in 2025?"*
and it answers from those documents, with the exact passages it used. You can also ask it to plan a business
trip. It finds real flights and hotels, checks them against the company travel policy, and asks before saving
anything. Once a trip is saved, it can draft an email to your manager or add the trip to your Google Calendar.

Everything runs on your own machine, and the whole thing fits on a modest GPU. There's no cloud LLM: the only
outside services are the flight search and, if you want it, Google Calendar.

![The Veltrix Copilot home page in dark mode: suggested questions and trips in the middle, the workspace panel with the latest trip, draft and event on the right](images/Veltrix-UI.png)

<sub>The home page (dark theme). Suggested questions to get you started, and the workspace panel on the right
that keeps your latest trip, email draft and calendar event in view.</sub>

---

## What's inside

Under the hood it's a small multi-agent system:

- **A router** reads your message and decides who should handle it. It's plain rules, no LLM, so it's fast
  and predictable.
- **Five agents** do the work: one answers from the documents (RAG), one plans trips, one writes email drafts,
  one manages the calendar, and one handles your saved trips.
- **LangGraph** runs them in order and merges their answers into a single reply.
- **Three MCP tool servers** are the only way the agents touch the outside world: flight and hotel search (via
  SerpAPI), email drafts (saved, never sent) and Google Calendar.
- **Ollama** runs the language model (`qwen3:4b`) and the embedding model (`nomic-embed-text`) locally.
- **Postgres + pgvector** stores the document passages, your chats and your trips.
- **FastAPI** is the backend; **Streamlit** is the web app.

One design choice runs through all of it: **the model only does what code can't.** Dates, prices, policy
checks, counting and picking flights are all handled in code. The model writes the itinerary, the email wording
and the final answer. That keeps a small 4B model honest.

---

## Before you start

You'll need:

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**, which installs the right Python (3.12) and
  the exact package versions for you.
- **[Ollama](https://ollama.com/download)** for the models.
- **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** for the database.
- **A [SerpAPI](https://serpapi.com/) key** for live flight and hotel prices. The free plan gives 250 searches a
  month, which is plenty.
- **A Google account**, if you want the calendar part. See [Google Calendar](#google-calendar) below.

Document questions and email drafts work without SerpAPI or Google, so you can start with just the first three.

---

## Getting it running

```powershell
# 1. Install everything (creates .venv; nothing to activate, `uv run` uses it automatically)
uv sync

# 2. Your settings
copy .env.example .env          # macOS/Linux: cp .env.example .env
#    then open .env and fill in SERPAPI_API_KEY, MANAGER_EMAIL, HOME_CITY and GOOGLE_CALENDAR_ID

# 3. The models
ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama pull nomic-embed-text

# 4. The database (wait until `docker compose ps` says "healthy")
docker compose up -d db

# 5. Load the documents (only needed once)
uv run -m veltrix_copilot.knowledge.ingest

# 6. Start everything: API, web app and the three tool servers
uv run scripts/dev.py
```

Then open **http://127.0.0.1:8501**. The API and its docs live at http://127.0.0.1:8000/docs.

A note on step 5: the documents come to about 3,300 passages, and each one gets embedded. On a GPU that takes a
few minutes; on CPU, go and make a coffee. It prints its progress as it goes. You only need to run it again if
you change files in `data/docs/` yourself. Documents you upload through the app are indexed automatically.

### Things to try

1. *What is the hotel cap in Singapore?*
2. *How many incidents happened in 2025?*
3. *Give me a report of the company's performance in the first quarter of 2025.*
4. *Plan a 3-day trip from Kochi to Berlin next month with a budget of $2500.* Then click **Yes** to save it,
   which also adds it to your calendar, or **No** to discard it.
5. *Create a draft email to my manager with the travel plan.*

---

## Finding your way around the app

The top bar has six pages:

- **Chat**: where you talk to it. Your past chats are in the sidebar, and a panel on the right shows your
  latest trip, email draft and calendar event. The panel stays put as you scroll. When the assistant asks a
  yes/no question, you get buttons instead of having to type.
- **Trips**: every trip you've saved. You can change the dates, number of nights, budget, flights or hotel,
  preview the change, and apply it.
- **Calendar**: the events the assistant added, plus your Google Calendar embedded.
- **Knowledge**: all the documents, by folder. Search them, upload new ones (PDF, Word, Markdown or text) or
  remove them.
- **Outbox**: the email drafts. Nothing is ever sent; copy a draft into your mail app when you're happy with it.
- **Status**: what's running and what isn't (the icon in the top bar turns red if something's down), the
  light/dark theme switch, and a reset button. Until you pick a theme, it follows your computer's setting.

---

## About the documents

Everything in `data/docs/` is **synthetic, AI-generated data**, created to practise and test retrieval. Veltrix
Systems doesn't exist, and every person, customer, email address, phone number and figure in the files is
invented. Any resemblance to a real company or person is a coincidence.

The corpus was generated to be internally consistent. An employee's manager, office and projects match across
their profile, their department page, the project pages and the meeting notes. That's what makes multi-hop
questions like *"Who manages the lead of Project Kestrel?"* answerable. Being generated, it can still contain
the odd contradiction or something that wouldn't happen in a real company, so treat it as test data, not as an
example of real policy or finance documents.

`evals/CORPUS_README.md` describes how the corpus is organised, and `evals/veltrix_qa.jsonl` holds the 488
questions (with their answers and source files) that came with it.

---

## How it answers questions

When you ask something, it searches the documents for the passages closest in meaning to your question and
gives the best few to the model. The model has to cite every fact, and citations it makes up are removed before
you see the answer. If nothing relevant turns up, it says so instead of guessing.

Plain similarity search has blind spots, though, so a few kinds of question get extra help from code first:

- **Counting and listing.** *"How many incidents in 2025?"* can't be answered from four passages; you need all
  of them. So the collection's index (`incidents/index.md`, `customers/index.md`, `projects/portfolio.md` and
  so on) is read in full, filtered to whatever you named (a year, *SEV1*, *ClimaTrack*, *Germany*…), and
  counted in code. The model just reports the number.
- **Quarters.** *"The first quarter of 2025"* goes straight to the right quarterly report. One thing to know:
  Veltrix's financial year runs **April to March**, so FY2025 Q1 is April–June 2024. The answer always spells
  out the months, and tells you to ask for FY2025 Q4 if you meant January–March 2025.
- **Things named by ID or file name**, like *INC-2025-0042*, *POL-FIN-001* or *fy2025-q1.md*, are opened
  directly.

Old versions of a document are kept but stay out of the way. Right now the only one is the archived v1.3
travel policy. It's only used if you specifically ask about it (*"what did the previous travel policy
say?"*).

If you want to see how well search is doing, there's a set of 488 test questions in `evals/`:

```powershell
uv run scripts/eval_retrieval.py                       # how often the right document is found
uv run scripts/eval_retrieval.py --k 8                 # same, giving the model more passages
uv run scripts/eval_retrieval.py --limit 50 --answers  # also checks the answers (slow)
```

---

## How it plans trips

You describe the trip in plain words. It works out the dates and searches for real flights and hotels, then
picks sensible ones: no impossible connections, a hotel under the policy cap, a return flight that leaves
enough time on the ground. The model writes a short day-by-day plan around them, and you get the costs, your
budget and any policy warnings.

Nothing is saved until you say **Yes**. Then the trip goes into your trips list and onto your Google Calendar.
If it clashes with something already there, it asks first. You can also say *save this trip* later in the
same chat, or *plan a trip to Pune and add it to my calendar* to do both at once.

The rules come from the Veltrix travel policy, **POL-FIN-001 v2.0**:

| | |
|---|---|
| Hotel cap per night | ₹9,000 India Tier-1 · ₹6,000 rest of India · $220 US · €180 EU · S$260 Singapore |
| Daily allowance | ₹4,500 · ₹3,000 · $75 · €60 · S$90 |
| Book ahead | 7 days for domestic trips, 21 days for international |
| Cabin | Economy. Premium economy on international flights over 6 hours. Business needs CFO approval |
| Approval | Your department head, above $5,000 |

These numbers live in `veltrix_copilot/travel/travel_rules.py`, and a test checks them against the policy
document, so the two can't drift apart.

The policy leaves two things open, so I had to make a call on both:

- **Which Indian cities are Tier-1?** The policy doesn't say. I went with Bangalore, Mumbai, Delhi, Chennai,
  Hyderabad, Kolkata, Pune and Ahmedabad. Change the list in `INDIA_TIER1` if yours is different.
- **What about the UK, the UAE, Japan…?** The policy has no limits for them. The planner uses the US amounts
  as a rough guide and clearly says the region isn't covered.

**About the flight search:** each new trip uses about 3 SerpAPI searches, and *show me other flights* uses 2.
Repeat searches within 24 hours come from a local cache for free. To protect your quota, the app stops at 100
searches a month (`SERPAPI_MONTHLY_LIMIT`) and tells you why. Prices are live but change all the time, and
nothing is ever booked.

---

## Google Calendar

This part takes about ten minutes, and you only do it once.

1. Go to the [Google Cloud console](https://console.cloud.google.com/), create a project, and turn on the
   **Google Calendar API** for it.
2. Set up the **OAuth consent screen**: choose *External*, leave it in *Testing* mode, and add your own Gmail
   address as a **test user**. If you skip that last bit, Google will block the sign-in.
3. Create an **OAuth client** of type **Desktop app**, download its JSON file, and save it as
   `secrets/google_client_secret.json` in this folder.
4. Run:

   ```powershell
   uv run scripts/google_auth.py
   ```

   Your browser opens, you sign in, and Google warns that it "hasn't verified this app". That's expected,
   because it's your own app. Click *Continue* and allow calendar access. The app only changes events it
   created itself; from your other events it reads just the titles and times, to spot clashes.

I'd recommend giving it **its own calendar**, so test trips don't clutter your real one. Create a calendar
called "Veltrix Copilot" in Google Calendar, copy its ID from *Settings → Integrate calendar*, and put it in
`.env` as `GOOGLE_CALENDAR_ID`.

Two things to know:

- In Testing mode, **Google signs you out after 7 days.** When the app says the calendar isn't connected, just
  run `uv run scripts/google_auth.py` again. The running app picks up the new sign-in by itself; no restart
  needed.
- If it still says you're not signed in, run `uv run scripts/google_auth.py --status`. It shows which file it's
  looking at and tests the connection with Google.

To disconnect completely, run `uv run scripts/google_auth.py --revoke`.

---

## Where things are

```
veltrix_copilot/
  core/          settings, model calls, data shapes, dates, currencies
  knowledge/     database, document loading, ingestion, old-version rules, lookups done in code
  travel/        policy numbers and checks, the trips store, trip editing
  agents/        the five agents
  orchestrator/  the router and the LangGraph graph
  tools/         the MCP client the agents use
  api/           the FastAPI app and chat history
mcp_servers/     the three tool servers (travel, email, calendar)
ui/              the Streamlit app: app.py, one file per page in views/, shared bits in components/
data/docs/       the 650 Veltrix documents
evals/           the 488 test questions
scripts/         dev.py (starts everything), google_auth.py, eval_retrieval.py
screenshots/     images used in this README
tests/           run them with: uv run pytest
```

The tests don't need Ollama, the internet or Google, so they run anywhere in a couple of seconds.

---

## When something goes wrong

**The icon next to Status in the top bar is red.** Open the Status page; it shows exactly which piece is down.

**`connection timeout expired` or `Can't reach Postgres`.** The database isn't running. Start Docker Desktop,
run `docker compose up -d db`, and wait for `docker compose ps` to say *healthy*. Also check that `.env` has
`DATABASE_URL=postgresql://app:app@127.0.0.1:5433/veltrix`. Note the port: **5433**, not 5432.

**"No documents ingested yet".** Run `uv run -m veltrix_copilot.knowledge.ingest`.

**"No SERPAPI_API_KEY set".** Add your key to `.env` and restart `uv run scripts/dev.py`.

**"Monthly safety limit reached".** You've used your SerpAPI allowance for the month. Wait, or raise
`SERPAPI_MONTHLY_LIMIT` in `.env`.

**"Google Calendar is not connected".** See [Google Calendar](#google-calendar) above.

**Answers are painfully slow.** Run `ollama ps`. It should say 100% GPU. If it doesn't, the model has spilled
onto the CPU; lower `NUM_CTX` in `.env` or use a smaller model.

**`ModuleNotFoundError`.** Run `uv sync`, and start commands with `uv run`.

---

*Veltrix Systems and everyone in its documents are fictional, and the documents are AI-generated. Any
resemblance to a real company or person is a coincidence.*