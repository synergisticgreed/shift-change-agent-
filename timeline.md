# Timeline

> One short entry per step. Newest at the bottom.

## Before Step 1 (2026-11-19 → 2026-11-20)
- Wrote `spec.md`, `architecture.md`, `design.md`, `README.md`.
- Built a first working core: FastAPI + LangGraph agent, SQLite (local), fake LLM, 19 tests.

## Step 1: Project setup & architecture (2026-11-20)
- **Stack:** FastAPI · LangGraph · SQLAlchemy (SQLite locally, Postgres in Docker) · PyJWT · Groq/fake LLM · pytest.
- **Layout:** `app/{auth, db, rules, tools, llm, agent}` plus `main.py`, `config.py` and `clock.py`, with one job per folder.
- **Main data flow:** Client → API (`main.py`) → Auth (JWT) → Service → Agent graph → Tools/Rules → DB.
- Added `Data Flow:` comments to every module.
- Bug pass: fixed same-cab requests, repeat requests wrongly flagged as duplicates, the resume guard, and the reply fact check.
- Added `timeline.md` (this file).

## Step 2: MVP — mock data & JWT identity (2026-11-20)
- **Mock data:** `data/seed.json` → SQLite. 2 companies with different cut-offs/seats. Added a `reset` command so seed dates never go stale.
- **JWT extraction:** token → checks (signature, expiry, role) → employee must exist in that company → `Identity`.
- **New endpoint:** `GET /me/trips` shows the request → JWT → mock data path end to end.
- Numbered `Data Flow [n]` comments along that path.

## Step 3: Simple frontend demo (2026-11-20)
- One static page (`app/static/index.html`, plain HTML + JS, no build step), served at `/`.
- 3 login buttons: Acme employee, Globex employee, Acme manager. Each gets a real signed JWT from `POST /demo/login/{persona}` (demo mode only).
- The page sends `Authorization: Bearer <jwt>` on every call: trips, chat, chat history, manager pending/approve/reject.
- Added `GET /chat/history` so the employee can see the manager's decision message.

## Step 4: Strict business rules & isolation (2026-11-20)
- **One rule path:** `rules/validate.py` decides allow/deny. `tools/checks.py` loads its inputs from the DB. The agent's pre-check and the save-time re-check both use it.
- **Rule order:** trip already departed → no free seat → cut-off. A departed trip can never be escalated or approved.
- **Last seat:** writes take the DB write lock before counting seats, so two people can't both get the last one.
- **Isolation:** every write also filters by `company_id`, and cross-company tests run against each tool.
- Split the booking nodes out of `nodes.py` into `agent/booking.py`.

## Step 5: Manager escalation & agent workflow (2026-11-20)
- **Pause → persist → resume:** commented end to end (`interrupt()` → SQLite checkpoint → `Command(resume)`).
- **Real restart test:** separate OS processes for employee → manager → employee, sharing only the DB files.
- **Safe resume:** one run per conversation at a time (per-thread lock). Decisions only apply to `pending_approval` requests (DB guard). A chat paused when the server crashed heals itself.
- **Agent robustness tests:** bad LLM JSON is retried once, an LLM outage falls back to the keyword parser, and Groq errors become `LLMError`.

## Step 6: Time & latency (2026-11-20)
- **Call cap:** `llm/budget.py` counts every LLM call per message (hard stop at 4; the fixed graph needs at most 3).
- **Time budget:** each message gets one LLM deadline (3.5 s). Every call's timeout is `min(1.5 s, time left)`, and when time runs out the code falls back (keyword parser / template).
- **Hard timeouts:** the Groq call has a wall-clock limit (not just httpx's per-phase timeouts) and capped `max_tokens`. DB lock waits are capped, and so is the wait for the per-conversation lock.
- **Proof:** latency tests with slow and hanging LLMs, plus `python -m eval.latency` for p50/p95.

## Step 7: Ambiguity & error handling (2026-11-20)
- **Clarifying:** bare answers ("drop", "tomorrow", "7 pm") now continue the pending question. The time question lists the real free cab times. "Pickup and drop" in one message asks which one.
- **Errors:** one helper (`app/errors.py`) logs the full trace on the server and gives the user a plain message. Bad input gets a plain 422. A crashed agent step no longer locks the conversation. Postgres gets statement/lock timeouts.

## Step 8: Docker & evaluation (2026-11-20)
- **One command:** `docker compose up --build` starts Postgres (app data, healthchecked) and the API (seeds on start, UI at :8000). The agent's checkpoints live in a named volume. A `.env` file is optional.
- **Bugs found by running the real stack:** Postgres rejected `col IS 'value'` (now `IS NOT DISTINCT FROM`). The container clock is UTC, which shifted "tomorrow" and cut-offs by 5½ h (now `APP_TZ`). The eval couldn't write its results as the non-root user. A missing `.env` broke `compose up`.
- **Verified in Docker:** fresh `up --build` with no `.env`; escalation → `docker compose restart api` → manager approves over HTTP; last-seat and double-submit races on Postgres. 98 tests and 22/22 eval cases pass inside the container.
