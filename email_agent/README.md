# 📬 Email Agent — AI-powered Gmail triage, newsletter digest & calendar automation

An automated personal assistant that reads my Gmail, **classifies every email** with an LLM,
labels it, **summarises newsletters** into a bilingual (English/Korean) web dashboard with charts,
and **adds university & job-interview dates to Google Calendar** — running hands-off every 30 minutes
in Docker, with a PostgreSQL database for analytics.

Built end-to-end on **free tiers** (Google Gemini API) — no paid services required.

---

## ✨ What it does

| Capability | Detail |
|---|---|
| 🏷️ **Classify & label** | Sorts each email into `uni / news / message / finance / shopping / real_estate / personal / junk / other`, with nested labels (e.g. `uni/study`, `message/Career`) applied straight to Gmail |
| 📰 **Newsletter dashboard** | Picks out newsletters, writes an **English + Korean** summary, and renders a web page with a **topic pie chart**, **per-topic keyword charts**, and an expandable full-email view — history is kept cumulatively |
| 📅 **Smart calendar** | Extracts dated actions and adds **only university deadlines/events and job-interview appointments** to Google Calendar (skips past dates, de-duplicates) |
| 🗄️ **Database** | Stores every email and event in **PostgreSQL** for SQL queries & analytics |
| 🤖 **Hands-off** | Packaged with Docker Compose; a scheduler runs the whole pipeline every 30 minutes |
| 💸 **Free & quota-aware** | Uses the free Gemini tier; batches all emails into **one AI request per run**, with a daily hard cap and a processed-cache so it never re-analyses the same mail |

---

## 🏗️ Architecture

```
                 ┌──────────────────────── Docker Compose ────────────────────────┐
   Gmail  ──▶    │  email-agent (Python, 30-min scheduler)                         │
 (+ forwarded    │    1. fetch new mail   2. ONE Gemini request (classify +        │
  university     │    newsletter + calendar)  3. label Gmail  4. build web page    │
  mail)          │    5. create calendar events  6. store in DB                    │
                 │            │                         │                          │
                 │            ▼                         ▼                          │
   Google  ◀─────│   results.html (charts + list)   PostgreSQL  ◀── db container   │
   Calendar ◀────│                                  (emails, events)               │
                 └────────────────────────────────────────────────────────────────┘
```

One batched LLM call per run does classification, newsletter summarisation, and calendar
extraction together — keeping the project comfortably inside the free Gemini quota.

---

## 🛠️ Tech stack

- **Language:** Python 3.12
- **AI / LLM:** Google Gemini API (structured JSON output via Pydantic schemas, prompt engineering)
- **APIs:** Gmail API, Google Calendar API (OAuth 2.0)
- **Database:** PostgreSQL 16 (psycopg)
- **Infra:** Docker & Docker Compose (multi-container: app + database)
- **Frontend:** self-contained HTML/CSS + inline SVG charts (no external dependencies)

---

## 🚀 Getting started

### Prerequisites
- Docker Desktop
- A Google account with the **Gmail API** and **Google Calendar API** enabled, and an OAuth
  **desktop** client → download it as `credentials.json` into this folder
- A free **Gemini API key** — https://aistudio.google.com/apikey

### Configure secrets
Copy the example and fill in your values (this file is git-ignored and never committed):

```bash
cp secrets.env.example secrets.env
# then edit secrets.env: set GEMINI_API_KEY and a POSTGRES_PASSWORD
```

### First-time login (creates token.json)
Run once on your machine to authorise Gmail/Calendar in the browser:

```bash
pip install -r requirements.txt
python run_all.py
```

### Run hands-off with Docker
```bash
docker compose up -d --build     # starts the app + PostgreSQL, runs every 30 min
docker compose logs -f           # watch it work
docker compose exec email-agent python db_stats.py   # query the database
```

The generated dashboard is written to `state/results.html`.

---

## 📁 Project structure

```
email_agent/
├── run_all.py            # orchestrator: one AI call → label + digest + calendar + DB
├── classify.py           # Gmail auth, email fetch, classification, label helpers
├── newsletter_digest.py  # newsletter web page: pie chart, keyword charts, list
├── calendar_sync.py      # Google Calendar event creation
├── db.py                 # PostgreSQL storage (emails, events)
├── db_stats.py           # SQL summary queries
├── scheduler.py          # 30-minute loop (container entrypoint)
├── Dockerfile
├── docker-compose.yml    # app + postgres, volumes for secrets/state/db
└── requirements.txt
```

---

## 🎯 Skills demonstrated

- **Business analysis:** requirements elicitation & iterative refinement, scope management,
  trade-off analysis (cost vs. capability, accuracy vs. free-tier limits)
- **Solution design:** MVP-first, staged delivery; process automation of a manual workflow
- **Engineering:** API integration, LLM prompt engineering & structured output, OAuth 2.0,
  data modelling (SQL), containerisation & orchestration, data visualisation
- **Security:** secrets kept out of the image & repo (env files / mounted volumes / `.gitignore`)

---

## 🔒 Security

Credentials (`credentials.json`, `token.json`), the API key, database password (`secrets.env`)
and all generated data (`state/`) are **git-ignored and never uploaded**. They are supplied at
runtime via environment variables and mounted volumes — never baked into the Docker image.

---

> Personal project. Built iteratively with real inbox data as a learning + portfolio piece.
