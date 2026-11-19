"""Single source of 'now' so tests can freeze time.
Data Flow: system clock -> now() -> cut-off rules (nodes.decide, tools.requests)."""
from datetime import datetime


def now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)
