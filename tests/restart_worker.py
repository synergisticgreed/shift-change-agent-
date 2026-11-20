"""One 'server lifetime' per OS process, used by test_restart.py.
Usage: python -m tests.restart_worker <phase> <workdir> [request_id]
Data Flow: fresh process -> env points at workdir DB files -> app starts (lifespan)
           -> runs one phase over HTTP -> prints JSON -> process exits (memory gone)."""
import json
import os
import sys
from datetime import datetime, time, timedelta

phase, workdir = sys.argv[1], sys.argv[2]
# Must be set BEFORE importing app: settings are read at import time
os.environ.update(DATABASE_URL=f"sqlite:///{workdir}/app.db",
                  CHECKPOINT_DB=f"{workdir}/checkpoints.db", LLM_PROVIDER="fake")

from fastapi.testclient import TestClient  # noqa: E402

from app import clock  # noqa: E402
from app.auth.jwt import issue_token  # noqa: E402
from app.main import app  # noqa: E402

# Freeze time at 17:00 on the seeded trip day: 1h before the 18:00 drop -> inside cut-off
TRIP_DAY = clock.real_today() + timedelta(days=1)   # seeded trip day
clock.now = lambda: datetime.combine(TRIP_DAY, time(17, 0))

EMP = {"Authorization": "Bearer " + issue_token("acme", "E101")}
MGR = {"Authorization": "Bearer " + issue_token("acme", "M1", "manager")}

with TestClient(app) as c:  # "with" runs startup: init_db + build graph from checkpoint file
    if phase == "employee_escalates":
        c.post("/chat", json={"message": "Move my drop today to 19:00"}, headers=EMP)
        out = c.post("/chat", json={"message": "yes"}, headers=EMP).json()
    elif phase == "manager_approves":
        out = c.post(f"/manager/requests/{sys.argv[3]}/decision",
                     json={"decision": "approve", "note": "ok after restart"}, headers=MGR).json()
    elif phase == "employee_checks":
        out = {"trips": c.get("/me/trips", headers=EMP).json()["trips"],
               "history": c.get("/chat/history", headers=EMP).json()}
    else:
        raise SystemExit(f"unknown phase {phase}")

print(json.dumps(out))
