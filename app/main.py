"""HTTP API (README: API table). Thin layer, no business logic.
Data Flow: Client -> API route -> Auth dependency (JWT) -> agent.service / tools -> JSON response."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import clock
from app.agent import service
from app.auth import demo
from app.auth.jwt import Identity, current_manager, current_user
from app.db.store import init_db
from app.errors import log
from app.tools import requests as rq
from app.tools import roster


@asynccontextmanager
async def lifespan(_):
    # Data Flow (startup): seed.json -> DB tables; checkpoint file -> agent graph
    init_db()
    service.get_graph()
    yield


# Server log: where every caught error's full trace goes (users only see plain text)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Shift-Change Request Agent", lifespan=lifespan)
app.include_router(demo.router)  # POST /demo/login/{persona}

UI_FILE = Path(__file__).parent / "static" / "index.html"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""


@app.exception_handler(RequestValidationError)
async def bad_input(_: Request, exc: RequestValidationError):
    """Data Flow: invalid body/params -> plain 422 (FastAPI's default lists internals)."""
    fields = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")})
    hint = f" Check: {', '.join(fields)}." if fields else ""
    return JSONResponse(status_code=422, content={"detail": "That request isn't valid." + hint})


@app.exception_handler(Exception)
async def plain_error(_: Request, exc: Exception):
    """FR-11 last line of defence. Data Flow: any uncaught error -> log trace -> plain 500."""
    log.error("unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again."})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def ui():
    # Data Flow: browser GET / -> static demo page (it then calls the API with a JWT)
    return FileResponse(UI_FILE)


@app.get("/me/trips")
def my_trips(who: Identity = Depends(current_user)):
    # Data Flow [1] Client GET + Bearer JWT -> [2] current_user: JWT -> Identity
    #           -> [3] roster.list_trips(Identity) -> mock data (SQLite) -> [4] JSON
    return {"company_id": who.company_id, "employee_id": who.user_id,
            "trips": roster.list_trips(who, clock.now().date().isoformat())}


@app.post("/chat")
def chat(body: ChatIn, who: Identity = Depends(current_user)):
    # Data Flow: {message} + JWT -> Identity -> service.chat -> {reply, request_id, status}
    return service.chat(who, body.message)


@app.get("/chat/history")
def chat_history(who: Identity = Depends(current_user)):
    # Data Flow: JWT -> Identity -> own thread checkpoint -> [{role, content}]
    return service.history(who)


@app.get("/requests/{request_id}")
def get_request(request_id: int, who: Identity = Depends(current_user)):
    req = rq.get_request(who, request_id)
    if not req:
        raise HTTPException(404, "Request not found.")  # same reply for other company's ids
    return req


@app.get("/manager/pending")
def pending(mgr: Identity = Depends(current_manager)):
    return rq.list_pending(mgr)


@app.post("/manager/requests/{request_id}/decision")
def decide(request_id: int, body: DecisionIn, mgr: Identity = Depends(current_manager)):
    # Data Flow: manager JWT + decision -> service.resume -> paused graph wakes -> DB updated
    try:
        out = service.resume(mgr, request_id, body.decision, body.note)
    except service.Busy:  # the employee's conversation is mid-run; don't make the manager wait
        raise HTTPException(409, "This request is being updated. Please try again in a moment.")
    if not out:
        raise HTTPException(404, "No pending request with that id.")
    return out
