# Shift-Change Request Agent

A chat agent that handles employee cab shift-change requests for multiple companies. It reads the employee's roster, enforces company rules in code, applies the change or offers the next option, and escalates to a manager when a rule is broken. The manager pause survives a restart.

**Docs:** [read.md](read.md) (full walkthrough: every file + end-to-end data flows) · [spec.md](spec.md) (what) · [architecture.md](architecture.md) (how it fits) · [design.md](design.md) (details) · [timeline.md](timeline.md) (build log)

---

## Quick start

```bash
docker compose up --build     # UI + API on http://localhost:8000 (fake LLM, no key needed)
# optional: cp .env.example .env and set LLM_PROVIDER=gemini (or groq) + LLM_API_KEY
```

| Task | Command |
|---|---|
| Local run (no Docker) | `pip install -r requirements.txt && uvicorn app.main:app`, then open http://localhost:8000 |
| Local tests | `pytest -q` |
| Fresh mock data (stop server first) | `python -m app.db.store reset` |
| Latency benchmark (p50/p95) | `python -m eval.latency 60` |
| Tests (fake LLM) | `docker compose run --rm api pytest -q` |
| Evaluation set (local) | `python -m eval.run` → `eval/cases.jsonl` + `EVALUATION.md` |
| Evaluation set (Docker) | `docker compose run --rm -v ./eval:/code/eval api python -m eval.run` |
| Stop / wipe demo data | `docker compose down` / `docker compose down -v` |
| Demo token | `docker compose run --rm api python -m app.auth.issue --company acme --employee E101` |

---

## Stack

| Part | Choice | Why |
|---|---|---|
| API | FastAPI | Typed, async, simple auth dependency |
| Agent | LangGraph | Clear flow + `interrupt()` for manager approval |
| Store | Postgres + SQLite checkpoint file | App data and audit log in Postgres; paused chats in a checkpoint file on a Docker volume |
| Auth | JWT | Carries `company_id` and `employee_id` |
| LLM | Groq `openai/gpt-oss-20b` (default) or Gemini, free tiers | Swappable; no key = fake LLM; tests use a fake |

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Demo UI (login buttons, chat, manager approvals) |
| `POST` | `/demo/login/{persona}` | Demo-only JWT for a fixed persona |
| `GET` | `/me/trips` | Employee sees their upcoming pickups/drops |
| `POST` | `/chat` | Employee sends a message |
| `GET` | `/chat/history` | Employee's conversation, including the manager's outcome |
| `GET` | `/requests/{id}` | Employee checks request status |
| `GET` | `/manager/pending` | Manager lists pending requests (own company) |
| `POST` | `/manager/requests/{id}/decision` | Manager approves/rejects → agent resumes |

---

## How it works

```
JWT ─► /chat ─► Understand (LLM) ─► Decide (code) ─► Reply (LLM)
                                        │
                               rule broken → pause ◄── manager decision
```

1. **Understand**: the LLM turns the message into a structured intent and asks a question if anything is unclear.
2. **Decide**: plain code checks the roster, cab seats and the company's cut-off rule.
3. **Reply**: the LLM phrases the outcome. It cannot change the outcome.

---

## Design decisions

- **Identity from the token only.** Tools never take company or employee IDs from the LLM.
- **Rules in code.** Cut-off and capacity checks are plain Python and are re-checked before saving.
- **LLM output is untrusted.** It's validated with Pydantic and retried once; if it's still invalid, the agent asks a question.
- **Durable pause.** LangGraph's checkpointer saves every step to a file on a Docker volume, so the paused conversation survives restarts.
- **Clear failures.** Timeouts, a full cab or a pending approval each get a plain next step. Full traces go to the server log only (`app/errors.py`), and the user never sees a stack trace.

---

## The two numbers

- **≤ 4 LLM calls:** 2 LLM steps plus at most 1 retry = 3. Every call goes through `llm/budget.ask()`, which refuses a 5th call.
- **p95 < 5 s:** one 3.5 s LLM budget per message, each call capped at `min(2 s, time left)` by the wall clock, and 300-token outputs. When time runs out, code takes over (keyword parser / template).
- **Measured:** `python -m eval.latency` → p50 0.86 s, p95 1.96 s, max 2 calls (simulated LLM). Every `/chat` reply carries `llm_calls` and `elapsed_ms`.

---

## Testing

The tests use a fake LLM, so they never depend on API quota. They cover:

- cross-company isolation
- cut-off rejection
- resume after restart
- tool selection
- tool failure
- ambiguous requests

`python -m eval.run` runs 22 edge cases, each on a fresh DB, and writes `eval/cases.jsonl` plus [EVALUATION.md](EVALUATION.md).

**Evaluation set ([eval/cases.jsonl](eval/cases.jsonl)):** one JSON line per case, always with the same fields:

| Field | Meaning |
|---|---|
| `id` | Case id (`E01`…`E22`) |
| `input` | The message(s) the employee sends |
| `expected` | The behaviour we want |
| `actual` | What the agent actually did (outcome + final DB state) |
| `pass` | `true` / `false` |
| `notes` | What the case tests, plus the max LLM calls and ms per message |

**How a case is judged:** it passes only if `actual` matches `expected`, checked against the reply outcome **and** the roster/request rows in the DB. Every message must also stay within 4 LLM calls and 5 s.

Example line:
```json
{"id": "E01", "input": "Move my drop tomorrow to 19:00", "expected": "applied, now on A-D2", "actual": "applied, now on A-D2", "pass": true, "notes": "Happy path. max 2 LLM calls, max 107 ms per message"}
```

---

## Bonus: concurrency

- **Last seat:** a row lock (`SELECT ... FOR UPDATE`) means only one employee gets the seat.
- **Double submit:** a unique `idempotency_key` means only one request is created.

---

## Trade-offs

| Choice | Gain | Cost |
|---|---|---|
| Fixed graph, not a free agent loop | Predictable calls and latency | Less flexible |
| Postgres + checkpoint file, no Redis | Few moving parts; tested checkpointer | Two stores (crash between them is handled: `wait_manager` is idempotent) |
| LLM writes replies | Natural tone | One extra call |
| Local HS256 JWT | Simple | Not production key management |

---

## Layout

```
app/        api, auth, agent, tools, rules, db
data/       seed data (2+ companies)
eval/       run.py (cases) + harness.py + latency.py; output cases.jsonl
tests/      pytest suite
```
