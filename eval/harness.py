"""A fresh, isolated copy of the whole app for ONE eval case.
Data Flow: Env() -> temp SQLite DB (seeded) + temp checkpoint file + frozen clock + LLM
           -> case talks HTTP to the real FastAPI app (TestClient) -> Env records
              the worst llm_calls / elapsed_ms seen, so every case also checks NFR-1/NFR-2."""
import tempfile
from datetime import datetime, time, timedelta

from fastapi.testclient import TestClient

from app import clock
from app.agent import service
from app.agent.graph import build_graph
from app.auth.jwt import Identity, issue_token
from app.db import store
from app.llm.fake import FakeLLM
from app.main import app
from app.tools.roster import get_trip

TODAY = clock.real_today()  # same local date the app seeds with
TRIP_DAY = TODAY + timedelta(days=1)       # seed puts every trip on tomorrow


class Env:
    def __init__(self, llm=None):
        self.dir = tempfile.mkdtemp(prefix="eval-")
        store.engine = store.make_engine(f"sqlite:///{self.dir}/app.db")
        store.init_db(store.engine)
        self.checkpoint = f"{self.dir}/cp.db"
        self.llm = llm or FakeLLM()
        service.graph = build_graph(self.llm, self.checkpoint)
        self.at(TODAY, 9, 0)               # default: morning before the trip day
        self.client = TestClient(app)
        self.max_calls = self.max_ms = 0

    # --- controls ---
    def at(self, day, h, m=0):
        t = datetime.combine(day, time(h, m))
        clock.now = lambda: t

    def late(self):
        """17:00 on the trip day: 1h before the 18:00 drop -> inside Acme's 2h cut-off."""
        self.at(TRIP_DAY, 17)

    def restart(self):
        """New agent graph on the same checkpoint file = what a server restart does."""
        service.graph = build_graph(FakeLLM(), self.checkpoint)

    # --- actions over HTTP ---
    def chat(self, company, emp, msg) -> dict:
        r = self.client.post("/chat", json={"message": msg}, headers=_auth(company, emp)).json()
        self.max_calls = max(self.max_calls, r.get("llm_calls", 0))
        self.max_ms = max(self.max_ms, r.get("elapsed_ms", 0))
        return r

    def decide(self, rid, decision="approve", company="acme", manager="M1"):
        return self.client.post(f"/manager/requests/{rid}/decision", json={"decision": decision},
                                headers=_auth(company, manager, "manager"))

    def get(self, path, company, user, role="employee"):
        return self.client.get(path, headers=_auth(company, user, role))

    # --- checks ---
    def cab(self, company, emp, trip="drop"):
        t = get_trip(Identity(company, emp, "employee"), TRIP_DAY.isoformat(), trip)
        return t and t["cab_id"]


def _auth(company, user, role="employee") -> dict:
    return {"Authorization": f"Bearer {issue_token(company, user, role)}"}
