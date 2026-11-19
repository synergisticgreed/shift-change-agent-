from datetime import datetime

from app.rules.engine import check_cutoff, has_seat, nearest_free


def test_cutoff_boundary():
    trip = datetime(2026, 1, 1, 20, 0)
    assert check_cutoff(120, trip, datetime(2026, 1, 1, 18, 0))      # exactly 2h: ok
    assert not check_cutoff(120, trip, datetime(2026, 1, 1, 18, 1))  # 1 min late


def test_has_seat():
    assert has_seat(4, 3) and not has_seat(4, 4)


def test_nearest_free_skips_full_cabs():
    opts = [{"depart_at": "20:00", "capacity": 4, "booked": 4},
            {"depart_at": "19:00", "capacity": 4, "booked": 0},
            {"depart_at": "22:30", "capacity": 4, "booked": 0}]
    assert nearest_free(opts, "20:30")["depart_at"] == "19:00"
