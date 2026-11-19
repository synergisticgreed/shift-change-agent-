"""Demo-only login for the UI buttons. Stands in for a real login system.
Data Flow: UI button -> POST /demo/login/{persona} -> fixed persona -> issue_token -> {token} -> UI.
Only these hardcoded personas can get a token, and only when DEMO_MODE is on."""
from fastapi import APIRouter, HTTPException

from app.auth.jwt import issue_token
from app.config import settings

router = APIRouter(prefix="/demo")

# persona -> (company_id, user_id, role, display name). All exist in data/seed.json.
PERSONAS = {
    "acme_employee": ("acme", "E101", "employee", "Asha (Acme employee)"),
    "globex_employee": ("globex", "G201", "employee", "Ivan (Globex employee)"),
    "acme_manager": ("acme", "M1", "manager", "Meera (Acme manager)"),
}


@router.post("/login/{persona}")
def demo_login(persona: str) -> dict:
    # Data Flow: persona name -> PERSONAS lookup -> signed JWT (same as real tokens)
    if not settings.demo_mode or persona not in PERSONAS:
        raise HTTPException(404, "Not found.")
    company, user, role, name = PERSONAS[persona]
    return {"token": issue_token(company, user, role), "name": name, "role": role}
