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
