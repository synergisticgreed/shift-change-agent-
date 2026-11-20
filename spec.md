# Spec: Shift-Change Request Agent

> What the system must do. If a requirement changes, update this file first.

## 1. Problem

Employees call the transport desk to change their cab time. The desk is slow, applies rules unevenly and sometimes mixes up companies. There is no clean record of who asked for what.

## 2. Goal

A chat agent that handles a shift-change request end to end, safely, for many companies on one service.

## 3. Users

| User | Wants to |
|---|---|
| Employee | Change, cancel or check their pickup/drop |
| Transport manager | Approve or reject requests that break a rule |

## 4. Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | Chat API protected by a JWT. The token carries `company_id` and `employee_id`. |
| FR-2 | Identity comes **only** from the token, never from the user or the LLM. |
| FR-3 | Agent can read the employee's roster, check cab options and raise a request. |
| FR-4 | Sample data for at least 2 companies with different cut-off and seat rules. |
| FR-5 | Rules are checked in code. A rule-breaking request is never applied. |
| FR-6 | When a rule is broken, explain why and offer the next option. |
| FR-7 | If the employee still wants it, escalate to the manager and pause. |
| FR-8 | A separate manager action resumes the paused chat, even after a restart. |
| FR-9 | Ask a clarifying question when the request is unclear ("a bit late"). |
| FR-10 | Keep context across messages ("make it 8 instead"). |
| FR-11 | On any failure, show a plain message with a next step. No stack traces, no hanging. |
| FR-12 | Keep an audit log of who asked and who decided. |
| FR-13 | A one-page demo UI with login buttons for a Company A employee, a Company B employee and a manager. It uses real JWTs. |

## 5. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | Reply within **5 s at p95** (LLM included). |
| NFR-2 | At most **4 LLM calls** per employee message. |
| NFR-3 | One employee can never see another company's data. |
| NFR-4 | Starts with one command: `docker compose up`. |
| NFR-5 | Tests run without a real LLM (fake LLM). |
| NFR-6 | No API keys in the repo. |

## 6. Acceptance criteria

- [x] Company A's token cannot read or change Company B's roster (test).
- [x] A request after the cut-off is rejected with a reason and an alternative (test).
- [x] Paused request resumes correctly after a service restart (test).
- [x] The agent calls the right tool for each kind of request (test).
- [x] A tool timeout gives a friendly message and changes nothing (test).
- [x] An unclear request gets a question, not a guess (test).
- [x] `eval/cases.jsonl` runs and has fields `id, input, expected, actual, pass, notes`.

## 7. Bonus (chosen)

- Two employees asking for the last seat at once: exactly one gets it.
- One employee double-submitting: exactly one request is created.

## 8. Out of scope

- A real MoveInSync backend (we use mock data in Postgres)
- A production frontend (only the FR-13 demo page)
- Real login (the demo page uses fixed personas)
- Production key management
