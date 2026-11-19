# Architecture

> How the pieces fit together. Covers spec items FR-1 to FR-12.

## 1. Components

| Component | Tech | Job |
|---|---|---|
| Demo UI | One HTML file + JS | Login buttons → JWT → calls the API (FR-13) |
| API | FastAPI | Routes, JWT check, error handling |
| Agent | LangGraph | Runs the conversation flow |
| LLM client | Groq / Gemini | Understands messages, writes replies |
| Tools | Python functions | Roster, cab options, requests |
| Rule engine | Plain Python | Cut-off and seat checks |
| Database | Postgres (SQLite locally) | Data, audit log, agent checkpoints |

## 2. Big picture

```
 Employee                          Manager
    │ JWT                             │ JWT (role=manager)
    ▼                                 ▼
┌──────────────────── FastAPI ─────────────────────┐
│  /chat                     /manager/.../decision │
└──────┬──────────────────────────────┬────────────┘
       ▼                              │ resume
┌────────────── LangGraph agent ──────▼────────────┐
│ understand → decide → reply                      │
│                │                                 │
│          rule broken → pause (interrupt)         │
└──────┬───────────────┬───────────────────────────┘
       ▼               ▼
   LLM client     Tools + Rule engine
                       │
                       ▼
                   Postgres  (data + checkpoints)
```

## 3. Chat flow

1. Employee sends a message with a JWT.
2. API reads `company_id` and `employee_id` from the token.
3. Agent loads the saved conversation state.
4. **LLM call 1:** message → structured intent.
5. If anything is missing → ask a question and stop.
6. Code checks the roster, seats and rules.
7. Allowed → apply the change. Not allowed → offer the next option or escalate.
8. **LLM call 2:** turn the result into a friendly reply.

## 4. Approval flow

1. Agent creates a `pending_approval` request and calls `interrupt()`.
2. LangGraph saves the state to Postgres. The service can restart safely.
3. Manager calls `/manager/requests/{id}/decision`.
4. API resumes the same thread with the decision.
5. Agent applies or rejects the change and records it in the audit log.

## 5. Safety boundaries

- **Tenant isolation:** every query is filtered by `company_id` from the token.
- **LLM sandbox:** the LLM only picks intent and writes text. It never sees IDs from other companies and cannot call the database directly.
- **Rules:** checked twice, once before the decision and again inside the save transaction.

## 6. Deployment

```
docker compose up
 ├── api   (FastAPI + LangGraph)
 └── db    (Postgres, seeded on start)
```

## 7. Folder structure

```
app/
  main.py        # FastAPI app
  config.py      # settings from .env
  clock.py       # "now" (frozen in tests)
  static/        # index.html demo UI
  auth/          # JWT, demo login
  agent/         # graph, nodes, state, intent, templates, service
  llm/           # Groq + fake LLM
  tools/         # roster (read), requests (write)
  rules/         # rule engine
  db/            # schema, seed
data/            # seed.json
eval/            # cases.jsonl, runner
tests/
```
