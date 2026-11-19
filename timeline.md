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
