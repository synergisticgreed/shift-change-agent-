"""One place for "something failed" (FR-11): full detail to the server log, plain text to the user.
Data Flow (every catch site):
    exception in a tool / node / route
      -> log.exception(...)      stack trace + error text -> server log only
      -> plain outcome           {"outcome": "tool_error"} -> template -> employee
    The employee never sees exception text, SQL, file paths or a traceback."""
import logging

log = logging.getLogger("shift_agent")


def tool_error(where: str) -> dict:
    """Call inside `except`: logs the active exception, returns the node's plain result."""
    log.exception("tool failed in %s", where)
    return {"result": {"outcome": "tool_error"}, "offer": None}
