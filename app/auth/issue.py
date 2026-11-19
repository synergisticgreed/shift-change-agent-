"""Demo token CLI: python -m app.auth.issue --company acme --employee E101 [--role manager]
Data Flow: CLI args -> issue_token -> JWT printed -> client sends it as Bearer header."""
import argparse

from app.auth.jwt import issue_token

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--company", required=True)
    p.add_argument("--employee", required=True)
    p.add_argument("--role", default="employee")
    a = p.parse_args()
    print(issue_token(a.company, a.employee, a.role))
