"""FR-8: a paused request survives a REAL restart (3 separate OS processes).
Data Flow: process 1 pauses + exits -> only app.db + checkpoints.db remain on disk
           -> process 2 (manager) resumes from disk -> process 3 (employee) sees the outcome."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*args) -> dict:
    res = subprocess.run([sys.executable, "-m", "tests.restart_worker", *map(str, args)],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_pause_survives_process_restart(tmp_path):
    # Process 1: employee breaks the cut-off, says "yes" -> paused, then the process exits
    first = _run("employee_escalates", tmp_path)
    assert first["status"] == "pending_approval"
    rid = first["request_id"]

    # Process 2: brand-new server; manager approves -> agent resumes from the checkpoint file
    second = _run("manager_approves", tmp_path, rid)
    assert second["status"] == "approved" and f"#{rid}" in second["employee_reply"]

    # Process 3: another new server; employee sees the change and the manager's message
    third = _run("employee_checks", tmp_path)
    assert ("drop", "19:00") in [(t["trip_type"], t["depart_at"]) for t in third["trips"]]
    assert "approved" in third["history"][-1]["content"]
