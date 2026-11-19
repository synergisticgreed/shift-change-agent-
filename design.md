# Design

> The details we build from: data, API, agent steps, rules and errors.

## 1. Data model

```
companies       (id, name)
policies        (company_id, trip_type, cutoff_minutes)
employees       (company_id, id, name, role)
cabs            (company_id, id, route, trip_type, depart_at "HH:MM", capacity)
roster          (company_id, employee_id, date "YYYY-MM-DD", trip_type, cab_id)
shift_requests  (id, company_id, employee_id, date, trip_type, from_cab, to_cab,
                 status, reason, thread_id, idempotency_key UNIQUE,
                 created_at, decided_by, decided_at)
audit_log       (id, company_id, actor, action, details, at)
```

- A cab runs daily at `depart_at`; seats booked = roster rows for that cab + date.
- `to_cab = NULL` means a cancel. `thread_id` is the chat to resume after approval.
- Local dev uses SQLite; Docker uses Postgres (same SQLAlchemy tables).

**Request status:** `applied`, `rejected`, `pending_approval`, `approved`, `declined`, `failed`

## 2. Sample companies

| Company | Cut-off | Seats per cab |
|---|---|---|
| `acme` | 2 h before | 4 |
| `globex` | 4 h before | 6 |

## 3. API

**`POST /chat`**
```json
// request
{ "message": "Can I leave at 8 tonight?" }
// response
{ "reply": "Done! Your drop is now at 20:00 on cab C12.",
  "request_id": 42, "status": "applied", "llm_calls": 2, "elapsed_ms": 640 }
```

**`POST /manager/requests/{id}/decision`**
```json
{ "decision": "approve", "note": "ok for today" }
```

Other endpoints: `GET /me/trips`, `GET /chat/history`, `GET /requests/{id}`, `GET /manager/pending`, `GET /health`.

**Demo UI (`GET /`)**: buttons call `POST /demo/login/{persona}` (`acme_employee`, `globex_employee`, `acme_manager`), which returns `{token, name, role}`.
It only works when `DEMO_MODE=true`; otherwise it returns 404. The page keeps the token in memory and sends it as `Bearer` on every call.

## 4. JWT claims

```json
{ "sub": "E101", "company_id": "acme", "role": "employee", "exp": 1700000000 }
```

A token is accepted only if: HS256 signature is valid, it is not expired, all 4 claims exist,
`role` is `employee|manager`, and `(company_id, sub, role)` exists in `employees`.
Any failure → the same `401 "Please log in again."`.

## 5. Agent state

```python
class AgentState(TypedDict):
    company_id: str        # from JWT
    employee_id: str       # from JWT
    messages: list         # chat history
    intent: Intent | None  # what the user wants
    result: dict | None    # outcome decided by code
    llm_calls: int         # budget counter
```

## 6. Intent (LLM output)

```python
class Intent(BaseModel):
    action: Literal["change_time", "cancel", "status", "confirm", "decline", "unclear"]
    trip_type: Literal["pickup", "drop"] | None
    date: date | None
    new_time: time | None
    missing: list[str] = []   # fields we still need to ask about
```

## 7. Agent nodes

| Node | LLM? | What it does |
|---|---|---|
| `understand` | Yes | Message → `Intent`, merged with the last intent (FR-10) |
| `clarify` | No | Asks about anything in `missing` |
| `status` | No | Latest request of this employee |
| `offer` | No | Handles "yes"/"no" to the last offer (alternative cab or escalate) |
| `decide` | No | Roster + cab options → `checks.precheck` (`booking.py`) |
| `apply` | No | Saves in one locked transaction, rules re-checked (`booking.py`) |
| `escalate` | No | Creates a pending request (`booking.py`) |
| `wait_manager` | No | Calls `interrupt()`; on resume applies or declines (`booking.py`) |
| `reply` | Yes | Rewrites the template draft; times/ids must survive or the draft is used |

`missing` is computed by code, not trusted from the LLM. If the LLM is down, `understand` falls back to a keyword parser so code still handles the request.

## 8. Rules

All rules live in code (`rules/validate.py`), never in an LLM prompt. The inputs come from the DB (`tools/checks.py`), filtered by the token's company.

```
change ─► load_facts (DB) ─► validate(facts) ─► None = allowed | reason
```

| Order | Reason | Rule | Next step for the employee |
|---|---|---|---|
| 1 | `stale` | Roster still shows the cab the change starts from | "Please send it again" |
| 2 | `trip_passed` | The old or new trip has not departed | None. Not even a manager can approve it. |
| 3 | `cab_full` | `booked (excluding me) < capacity` | Offer the nearest cab with a free seat (not my current cab) |
| 4 | `cutoff` | Earliest of old/new trip is ≥ `cutoff_minutes` away | Offer to escalate to the manager |

- The same `validate()` runs twice: as a pre-check in `booking.decide` and again inside the locked save transaction in `requests.apply_change`.
- A manager approval sets `override_cutoff=True`. That skips rule 4 only, so seats and departed trips are still enforced.
- Seats are checked before the cut-off, so a full cab is never escalated.

## 9. LLM budget (≤ 4)

| Call | When | If refused / fails |
|---|---|---|
| 1 | `understand` | keyword parser |
| 2 | `understand` retry (only if output is invalid JSON) | keyword parser |
| 3 | `reply` (not for questions/errors) | template draft as is |

The fixed graph makes at most 3 calls. Every call goes through `llm/budget.ask()`, which refuses a call (`BudgetExceeded`) when:
- 4 calls were already made for this message, or
- less than 0.3 s is left before `state["deadline"]`.

**Time limits (p95 < 5 s, NFR-1)**

| Limit | Value | Where |
|---|---|---|
| All LLM calls of one message | 3.5 s (`LLM_BUDGET_S`) | `state["deadline"]` stamped in `service.chat` (and on manager resume) |
| One LLM call (wall clock) | `min(1.5 s, time left)` | `budget.ask` → `GroqLLM` worker thread |
| LLM output length | 150 tokens | `max_tokens` |
| Wait for the conversation lock | 1 s → "busy" reply / 409 | `service._hold` |
| Wait for a SQLite lock | 1.5 s | `busy_timeout` |
| Our own code (measured) | p95 39 ms | auth + DB + rules + checkpoints |

Measured with `python -m eval.latency` (simulated LLM with a 3% stall rate): p50 0.86 s, p95 1.96 s, max 2 calls.
Worst case in tests (slow LLM + retry): 3.55 s.

## 10. Error messages

| Case | Employee sees |
|---|---|
| Tool timeout | "I couldn't reach the roster system. Nothing was changed. Please try again in a minute." |
| Cab just filled | "That seat was just taken. The next free slot is 20:30. Want it?" |
| Waiting for manager | "Your request #42 is with your transport manager. I'll update you here." |
| Approved but cab full / left | "Your manager approved request #42, but the cab filled up or already left, so your trip stays as it was." |
| Manager clicks twice | Second click gets `404 "No pending request with that id."` (or `409` if the first is still running) |
| LLM down | Template reply, and the request is still handled by code |
| Agent step crashes (a bug) | Same plain tool message; next message starts a fresh run (chat never stuck) |
| Bad request body | `422 "That request isn't valid. Check: message."` |
| Anything uncaught | `500 "Something went wrong. Please try again."` |

**How errors are caught (FR-11)**

```
tool / DB error in a node ─► errors.tool_error(where) ─► log trace ─► {"outcome": "tool_error"} ─► template
error in the graph itself  ─► service.chat except     ─► log trace ─► plain reply, status "failed"
bad input                  ─► RequestValidationError  ─► plain 422
anything else              ─► app exception handler   ─► log trace ─► plain 500
```

Writes run in one transaction, so any failure rolls back and nothing is half-changed. Postgres also gets `statement_timeout=2s` and `lock_timeout=1.5s`.

**Clarifying questions (FR-9):** code computes `missing`, so the agent asks for one field per turn (trip → day → time). The time question lists the free cab times. A bare answer ("drop", "tomorrow", "7 pm") continues the same request through `intent.merge`. A message about both pickup and drop asks which one.
| Bad token | `401`: "Please log in again." |

## 11. Concurrency (bonus)

- Writes use `store.write_txn()`, which takes the lock **before** counting seats:
  SQLite runs `BEGIN IMMEDIATE`, and Postgres runs `SELECT ... FOR UPDATE` on the cab row.
  A threaded test proves exactly one of two people gets the last seat.
- `idempotency_key = hash(company, employee, date, trip, from_cab, to_cab, seq)` with a `UNIQUE` constraint.
  `seq` = requests already made for that trip: a double submit shares `seq` (blocked), a later repeat doesn't (allowed).

## 12. Change log

| Date | Change |
|---|---|
| 2026-11-19 | First version |
| 2026-11-20 | Core built. Added `decline` action; `status`, `offer`, `wait_manager` nodes; request `date/trip_type/reason/thread_id`; dropped `policies.seat_capacity` (cab has capacity); SQLite for local dev |
| 2026-11-20 | Step 1 bug pass: `same_time` + `approved_cancel` outcomes; offers expire after one turn; alternatives skip your own cab; `seq` in idempotency key; resume checks the thread is paused on that request |
| 2026-11-20 | Step 2: JWT role + employee-exists checks; `GET /me/trips`; `python -m app.db.store reset` |
| 2026-11-20 | Step 3: demo UI at `/`, `POST /demo/login/{persona}` (DEMO_MODE), `GET /chat/history` |
| 2026-11-20 | Step 4: one rule path (`validate` + `load_facts`); new `trip_passed`/`stale` outcomes; seats before cut-off; locked write txns; company filter on approval update; booking nodes in `agent/booking.py` |
| 2026-11-20 | Step 5: per-thread run lock; decisions only on `pending_approval`; idempotent `wait_manager` + self-healing paused chat; `failed` outcome; manager note in audit; multi-process restart test |
| 2026-11-20 | Step 6: `llm/budget.py` (call cap + per-message deadline); Groq wall-clock limit + `max_tokens`; lock waits capped; `busy` outcome / 409; `llm_calls` + `elapsed_ms` in `/chat`; `eval/latency.py` |
| 2026-11-20 | Step 7: bare answers continue a request; time question lists free cabs; both-trips asks; `app/errors.py` (log + plain); paused = has interrupt (crash no longer wedges chat); plain 422; Postgres timeouts |
