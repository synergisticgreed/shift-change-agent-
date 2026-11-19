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

```
PAUSE    escalate: DB row "pending_approval" ─► wait_manager: interrupt()
PERSIST  state {change, request_id, messages} ─► checkpoint file, key "company:employee"
         (server can stop here; the employee's /chat now replies "with your manager")
RESUME   POST /manager/requests/{id}/decision ─► service.resume
         ─► checks: manager's company, still pending, thread paused on THIS id
         ─► graph.invoke(Command(resume=decision)) ─► wait_manager runs again from the top
         ─► approve: apply_change (cut-off overridden; seats + departed still checked)
            reject:  close_request ─► reply appended to the employee's chat
```

Safety on resume:
- **One run per conversation:** a per-thread lock in `service.py`, so a double click or a message at the same moment waits its turn.
- **Decide once:** an approval or rejection only applies while the DB status is still `pending_approval`.
- **Crash replay:** `wait_manager` reads the DB status first and never applies twice. If the server died between the DB commit and the checkpoint save, the employee's next message resumes the thread and reports the recorded outcome.
- Proven by `tests/test_restart.py` (3 separate OS processes) and `tests/test_escalation.py`.

## 5. Safety boundaries

- **Tenant isolation:** every read **and write** is filtered by `company_id` from the token. Asking for another company's cab or request finds nothing (`NoResultFound` / 404). Cab foreign keys include `company_id`.
- **LLM sandbox:** the LLM only picks intent and writes text. Its prompts contain no rules, no roster and no other company's data, and it can't call the database.
- **Rules:** one function (`rules/validate.py`) checked twice: once before the decision and again inside the locked save transaction.

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
  agent/         # graph, nodes (chat), booking (rules + writes), state, intent, templates, service
  llm/           # Groq + fake LLM
  tools/         # roster (read), checks (rule facts), requests (write)
  rules/         # engine (small checks), validate (allow/deny)
  db/            # schema, seed
data/            # seed.json
eval/            # cases.jsonl, runner
tests/
```
