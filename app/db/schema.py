"""Tables (design.md §1). Every table carries company_id for tenant isolation.
Data Flow: store.init_db creates these -> tools/* read & write them -> API returns rows."""
from sqlalchemy import (Column, DateTime, ForeignKeyConstraint, Integer,
                        MetaData, String, Table, Text, UniqueConstraint, func)

meta = MetaData()

companies = Table(
    "companies", meta,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
)

# One rule set per company + trip type
policies = Table(
    "policies", meta,
    Column("company_id", String, primary_key=True),
    Column("trip_type", String, primary_key=True),  # pickup | drop
    Column("cutoff_minutes", Integer, nullable=False),
)

employees = Table(
    "employees", meta,
    Column("company_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("role", String, nullable=False),  # employee | manager
)

# A cab runs every day at depart_at ("HH:MM")
cabs = Table(
    "cabs", meta,
    Column("company_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("route", String, nullable=False),
    Column("trip_type", String, nullable=False),
    Column("depart_at", String, nullable=False),
    Column("capacity", Integer, nullable=False),
)

# Who rides which cab on which date ("YYYY-MM-DD")
roster = Table(
    "roster", meta,
    Column("company_id", String, primary_key=True),
    Column("employee_id", String, primary_key=True),
    Column("date", String, primary_key=True),
    Column("trip_type", String, primary_key=True),
    Column("cab_id", String, nullable=False),
    ForeignKeyConstraint(["company_id", "cab_id"], ["cabs.company_id", "cabs.id"]),
)

shift_requests = Table(
    "shift_requests", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", String, nullable=False),
    Column("employee_id", String, nullable=False),
    Column("date", String, nullable=False),
    Column("trip_type", String, nullable=False),
    Column("from_cab", String),
    Column("to_cab", String),  # None = cancel
    Column("status", String, nullable=False),
    Column("reason", String),
    Column("thread_id", String, nullable=False),  # agent conversation to resume
    Column("idempotency_key", String, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
    Column("decided_by", String),
    Column("decided_at", DateTime),
    UniqueConstraint("idempotency_key"),  # blocks double submit
)

audit_log = Table(
    "audit_log", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("action", String, nullable=False),
    Column("details", Text),
    Column("at", DateTime, server_default=func.now()),
)
