"""Shared fixtures: fresh DB, fake LLM, frozen clock, fresh agent per test."""
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.agent import service
from app.agent.graph import build_graph
from app.auth.jwt import issue_token
from app.db import store
from app.llm.fake import FakeLLM
from app.main import app

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """Everything a test needs. ctx.set_now() moves the clock."""
    eng = store.make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(store, "engine", eng)
    store.init_db(eng)

    now = {"t": datetime.combine(TODAY, time(9, 0))}
    monkeypatch.setattr(clock, "now", lambda: now["t"])

    llm = FakeLLM()
    cp = str(tmp_path / "cp.db")
    monkeypatch.setattr(service, "graph", build_graph(llm, cp))

    class Ctx:
        client = TestClient(app)
        fake = llm
        checkpoint = cp

        @staticmethod
        def set_now(dt):
            now["t"] = dt

        @staticmethod
        def chat(company, emp, msg):
            res = Ctx.client.post("/chat", json={"message": msg}, headers=auth(company, emp))
            assert res.status_code == 200
            return res.json()

    return Ctx


def auth(company: str, user: str, role: str = "employee") -> dict:
    return {"Authorization": f"Bearer {issue_token(company, user, role)}"}


def booked_cab(company: str, emp: str, trip: str = "drop", day: str = TOMORROW):
    from app.auth.jwt import Identity
    from app.tools.roster import get_trip
    t = get_trip(Identity(company, emp, "employee"), day, trip)
    return t and t["cab_id"]
