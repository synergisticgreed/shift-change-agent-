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
  "request_id": 42, "status": "applied" }
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
| `decide` | No | Roster + cab options + rule check |
| `apply` | No | Saves the change in one transaction (rules re-checked) |
| `escalate` | No | Creates a pending request |
| `wait_manager` | No | Calls `interrupt()`; on resume applies or declines |
| `reply` | Yes | Rewrites the template draft; times/ids must survive or the draft is used |

`missing` is computed by code, not trusted from the LLM. If the LLM is down, `understand` falls back to a keyword parser so code still handles the request.

## 8. Rules

```python
def check_cutoff(policy, scheduled_at, now) -> bool:
    # change must come at least cutoff_minutes before the trip
    return scheduled_at - now >= timedelta(minutes=policy.cutoff_minutes)

def has_seat(cab, booked) -> bool:
    return booked < cab.capacity
```

If there's no seat, offer the nearest cab time that has one.

## 9. LLM budget (≤ 4)

| Call | When |
|---|---|
| 1 | `understand` |
| 2 | `understand` retry (only if output is invalid) |
| 3 | `reply` |

Max 3. If `llm_calls` reaches 4, use a template reply.

## 10. Error messages

| Case | Employee sees |
|---|---|
| Tool timeout | "I couldn't reach the roster system. Nothing was changed. Please try again in a minute." |
| Cab just filled | "That seat was just taken. The next free slot is 20:30. Want it?" |
| Waiting for manager | "Your request #42 is with your transport manager. I'll update you here." |
| LLM down | Template reply, and the request is still handled by code |
| Bad token | `401`: "Please log in again." |

## 11. Concurrency (bonus)

- `SELECT ... FOR UPDATE` on the cab row before booking a seat.
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
