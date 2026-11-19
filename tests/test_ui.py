"""Step 3: demo UI backend pieces.
Data Flow (tested): UI button -> /demo/login/{persona} -> JWT -> Bearer header -> API routes."""
from datetime import datetime, time, timedelta

import pytest

from app.auth.demo import PERSONAS
from app.auth.jwt import decode_token
from app.config import settings
from tests.conftest import TODAY, auth


def _login(ctx, persona):
    res = ctx.client.post(f"/demo/login/{persona}")
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_page_is_served(ctx):
    res = ctx.client.get("/")
    assert res.status_code == 200 and "Shift-Change Request Agent" in res.text


@pytest.mark.parametrize("persona, company, user, role", [
    ("acme_employee", "acme", "E101", "employee"),
    ("globex_employee", "globex", "G201", "employee"),
    ("acme_manager", "acme", "M1", "manager"),
])
def test_each_button_gets_the_right_jwt(ctx, persona, company, user, role):
    token = ctx.client.post(f"/demo/login/{persona}").json()["token"]
    who = decode_token(token)
    assert (who.company_id, who.user_id, who.role) == (company, user, role)


def test_every_persona_exists_in_seed(ctx):
    # A persona missing from seed.json would get a 401 on every call
    for persona in PERSONAS:
        hdr = _login(ctx, persona)
        url = "/manager/pending" if PERSONAS[persona][2] == "manager" else "/me/trips"
        assert ctx.client.get(url, headers=hdr).status_code == 200


def test_unknown_persona_is_404(ctx):
    assert ctx.client.post("/demo/login/root").status_code == 404


def test_demo_login_off_in_production(ctx, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    assert ctx.client.post("/demo/login/acme_employee").status_code == 404


def test_ui_flow_employee_escalates_manager_approves(ctx):
    """The exact click path in the UI, end to end."""
    ctx.set_now(datetime.combine(TODAY + timedelta(days=1), time(17, 0)))  # inside cut-off
    emp, mgr = _login(ctx, "acme_employee"), _login(ctx, "acme_manager")

    ctx.client.post("/chat", json={"message": "Move my drop today to 19:00"}, headers=emp)
    rid = ctx.client.post("/chat", json={"message": "yes"}, headers=emp).json()["request_id"]

    pending = ctx.client.get("/manager/pending", headers=mgr).json()
    assert [r["id"] for r in pending] == [rid]
    out = ctx.client.post(f"/manager/requests/{rid}/decision",
                          json={"decision": "approve"}, headers=mgr).json()
    assert out["status"] == "approved"

    # Employee re-opens the page: history now ends with the manager's outcome
    history = ctx.client.get("/chat/history", headers=emp).json()
    assert history[-1]["role"] == "assistant" and "approved" in history[-1]["content"]


def test_globex_manager_sees_no_acme_requests(ctx):
    ctx.set_now(datetime.combine(TODAY + timedelta(days=1), time(17, 0)))
    emp = _login(ctx, "acme_employee")
    ctx.client.post("/chat", json={"message": "Move my drop today to 19:00"}, headers=emp)
    ctx.client.post("/chat", json={"message": "yes"}, headers=emp)
    assert ctx.client.get("/manager/pending", headers=auth("globex", "M2", "manager")).json() == []


def test_history_is_per_employee(ctx):
    _ = ctx.client.post("/chat", json={"message": "any update?"}, headers=_login(ctx, "acme_employee"))
    assert ctx.client.get("/chat/history", headers=_login(ctx, "globex_employee")).json() == []
