"""Step 2: JWT identity extraction + mock data layer.
Data Flow (each test): token -> GET /me/trips -> current_user -> roster.list_trips -> JSON."""
import time

import jwt
import pytest
from sqlalchemy import func, select

from app.auth.jwt import ALGO, issue_token
from app.config import settings
from app.db import store
from app.db.schema import cabs, companies, policies, shift_requests
from tests.conftest import TOMORROW, auth


def _get(ctx, token):
    return ctx.client.get("/me/trips", headers={"Authorization": f"Bearer {token}"})


def _raw(**claims):
    base = {"sub": "E101", "company_id": "acme", "role": "employee", "exp": int(time.time()) + 60}
    return jwt.encode({**base, **claims}, settings.jwt_secret, algorithm=ALGO)


# ---------- JWT extraction ----------

def test_valid_token_gives_own_trips(ctx):
    body = _get(ctx, issue_token("acme", "E101")).json()
    assert (body["company_id"], body["employee_id"]) == ("acme", "E101")
    assert [(t["trip_type"], t["depart_at"]) for t in body["trips"]] == [("pickup", "08:00"), ("drop", "18:00")]
    assert all(t["date"] == TOMORROW for t in body["trips"])


@pytest.mark.parametrize("token", [
    "not-a-jwt",
    _raw(exp=int(time.time()) - 1),                                   # expired
    jwt.encode({"sub": "E101", "company_id": "acme", "role": "employee",
                "exp": int(time.time()) + 60}, "wrong-secret-wrong-secret-wrong!!", algorithm=ALGO),
    _raw(role="admin"),                                               # unknown role
    _raw(company_id=None),                                            # missing value
    _raw(sub="E999"),                                                 # not in DB
    _raw(company_id="globex"),                                        # E101 isn't Globex's
    _raw(role="manager"),                                             # E101 isn't a manager
])
def test_bad_tokens_get_plain_401(ctx, token):
    res = _get(ctx, token)
    assert res.status_code == 401 and res.json()["detail"] == "Please log in again."


def test_missing_header_is_401(ctx):
    assert ctx.client.get("/me/trips").status_code == 401


def test_alg_none_token_rejected(ctx):
    token = jwt.encode({"sub": "E101", "company_id": "acme", "role": "employee",
                        "exp": int(time.time()) + 60}, None, algorithm="none")
    assert _get(ctx, token).status_code == 401


def test_employee_cannot_use_manager_routes(ctx):
    assert ctx.client.get("/manager/pending", headers=auth("acme", "E101")).status_code == 403


# ---------- mock data layer ----------

def test_seed_has_two_companies_with_different_rules(ctx):
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(companies)).scalar_one() == 2
        cut = dict(conn.execute(select(policies.c.company_id, policies.c.cutoff_minutes)
                                .where(policies.c.trip_type == "drop")).all())
        seats = dict(conn.execute(select(cabs.c.company_id, func.max(cabs.c.capacity))
                                  .group_by(cabs.c.company_id)).all())
    assert cut == {"acme": 120, "globex": 240}
    assert seats == {"acme": 4, "globex": 6}


def test_each_company_sees_only_its_own_trips(ctx):
    acme = _get(ctx, issue_token("acme", "E101")).json()["trips"]
    globex = _get(ctx, issue_token("globex", "G201")).json()["trips"]
    assert {t["cab_id"][0] for t in acme} == {"A"} and {t["cab_id"][0] for t in globex} == {"G"}


def test_reset_restores_fresh_seed(ctx):
    ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")   # creates a request
    store.reset_db(store.engine)
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(shift_requests)).scalar_one() == 0
    trips = _get(ctx, issue_token("acme", "E101")).json()["trips"]
    assert ("drop", "18:00") in [(t["trip_type"], t["depart_at"]) for t in trips]
