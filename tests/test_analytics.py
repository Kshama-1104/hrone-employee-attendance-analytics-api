import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import mongomock
import pymongo
import pytest
from fastapi.testclient import TestClient

import app.main as app_module
from app.main import app


@pytest.fixture
def mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    test_db = mock_client["test_hrone"]
    monkeypatch.setattr(app_module, "db", test_db)
    app_module.ensure_indexes()
    return test_db


@pytest.fixture
def client(mock_db):
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def test_employee_monthly_unknown_employee_404(client):
    res = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404
    assert res.json() == {"detail": "Unknown employee"}


def test_employee_monthly_invalid_months_422(client):
    # Regex rejects invalid month formats
    assert client.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly?month=2026-00").status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly?month=invalid").status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_employee_monthly_normal_summary(client, mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # July 2026: 31 days, starts Wed 2026-07-01, ends Fri 2026-07-31.
    # Total Mon-Fri in July 2026: 23 working days.
    # Let's seed attendance logs:
    # 2026-07-06 (Mon): PRESENT, 0 late, 0 overtime, full day
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-07 (Tue): WFH, half_day=True, late_minutes=15, overtime_minutes=35
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "WFH",
        "work_hours": 4.0,
        "late_minutes": 15,
        "overtime_minutes": 35,
        "half_day": True,
    })
    # 2026-07-08 (Wed): ON_DUTY, full day
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-08",
        "status": "ON_DUTY",
        "work_hours": 8.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-09 (Thu): LEAVE
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-09",
        "status": "LEAVE",
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-10 (Fri): ABSENT
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-10",
        "status": "ABSENT",
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    # 2026-07-11 (Sat): Weekend PRESENT (adds 0 to present_days, but late/ot count!)
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-11",
        "status": "PRESENT",
        "work_hours": 5.0,
        "late_minutes": 20,
        "overtime_minutes": 45,
        "half_day": False,
    })

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    # working_days in July 2026: 23
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    # present_days: 1.0 (Mon) + 0.5 (Tue) + 1.0 (Wed) + 0 (Thu leave) + 0 (Fri absent) + 0 (Sat weekend) = 2.5
    assert data["present_days"] == 2.5
    assert data["leave_days"] == 1
    # late_count: Tue (15) + Sat (20) = 2
    assert data["late_count"] == 2
    assert data["total_late_minutes"] == 35
    # total_overtime_minutes: Tue (35) + Sat (45) = 80
    assert data["total_overtime_minutes"] == 80
    # attendance_pct: 2.5 / 23 * 100 = 10.869565... -> 10.87
    assert data["attendance_pct"] == 10.87


def test_employee_monthly_mid_month_joiner(client, mock_db):
    # Joined on 2026-07-27 (Monday). July ends on Friday 2026-07-31.
    # Working days: 2026-07-27 to 2026-07-31 = 5 working days (Mon-Fri).
    mock_db.employees.insert_one({
        "emp_code": "EMP0002",
        "name": "Bob Mid",
        "email": "bob@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-27",
    })

    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": "2026-07-27",
        "status": "PRESENT",
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })

    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 5
    assert data["present_days"] == 1.0
    # 1.0 / 5 * 100 = 20.0
    assert data["attendance_pct"] == 20.0


def test_employee_monthly_joined_after_month(client, mock_db):
    # Joined on 2026-08-01. Requested month is 2026-07.
    # Working days = 0, attendance_pct = null.
    mock_db.employees.insert_one({
        "emp_code": "EMP0003",
        "name": "Charlie Future",
        "email": "charlie@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    })

    res = client.get("/analytics/employees/EMP0003/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 0
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] is None


def test_employee_monthly_zero_logs(client, mock_db):
    # Joined before month, but has zero attendance logs.
    mock_db.employees.insert_one({
        "emp_code": "EMP0004",
        "name": "Diana Zero",
        "email": "diana@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    res = client.get("/analytics/employees/EMP0004/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["attendance_pct"] == 0.0


# --------------------------------------------------------------------------- #
# 2. GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def test_department_summary_includes_zero_log_employees_and_excludes_later(client, mock_db):
    # Department: Engineering has 2 employees who joined on/before 2026-07-31:
    # EMP0001 (has logs) and EMP0002 (zero logs).
    # EMP0003 joined in August 2026 (must be excluded from July summary).
    # Department: Sales has 1 employee who joined in July.
    mock_db.employees.insert_many([
        {
            "emp_code": "EMP0001",
            "name": "Eng One",
            "department": "Engineering",
            "joined_on": "2026-01-01",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        {
            "emp_code": "EMP0002",
            "name": "Eng Two",
            "department": "Engineering",
            "joined_on": "2026-07-15",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        {
            "emp_code": "EMP0003",
            "name": "Eng Future",
            "department": "Engineering",
            "joined_on": "2026-08-01",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
        {
            "emp_code": "EMP0004",
            "name": "Sales Person",
            "department": "Sales",
            "joined_on": "2026-05-01",
            "shift_start": "09:30",
            "shift_end": "18:30",
        },
    ])

    # Seed logs for EMP0001 only (EMP0002 has zero logs)
    # 2026-07-06 (Mon): PRESENT, work_hours=8.5, late_minutes=15
    # 2026-07-07 (Tue): PRESENT, work_hours=9.0, late_minutes=0
    # 2026-07-08 (Wed): ON_DUTY, work_hours=7.5, late_minutes=0
    # 2026-07-09 (Thu): LEAVE, work_hours=None
    mock_db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.5,
            "late_minutes": 15,
            "half_day": False,
        },
        {
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "work_hours": 9.0,
            "late_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP0001",
            "date": "2026-07-08",
            "status": "ON_DUTY",
            "work_hours": 7.5,
            "late_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP0001",
            "date": "2026-07-09",
            "status": "LEAVE",
            "work_hours": None,
            "late_minutes": 0,
            "half_day": False,
        },
    ])

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    # Engineering and Sales
    assert len(items) == 2
    eng = next(item for item in items if item["department"] == "Engineering")
    sales = next(item for item in items if item["department"] == "Sales")

    # Engineering headcount: EMP0001 and EMP0002 (EMP0003 excluded) -> 2
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0  # Mon (1) + Tue (1) + Wed (1)
    # avg_work_hours: (8.5 + 9.0 + 7.5) / 3 = 25.0 / 3 = 8.3333... -> 8.33
    assert eng["avg_work_hours"] == 8.33
    assert eng["late_count"] == 1
    assert eng["total_late_minutes"] == 15
    assert eng["leave_count"] == 1
    assert eng["on_duty_count"] == 1

    # Sales has 1 headcount, 0 logs
    assert sales["headcount"] == 1
    assert sales["present_days"] == 0.0
    assert sales["avg_work_hours"] is None
    assert sales["late_count"] == 0
    assert sales["total_late_minutes"] == 0


def test_department_summary_department_filter_exact_match(client, mock_db):
    mock_db.employees.insert_many([
        {"emp_code": "EMP0001", "name": "E1", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "E2", "department": "Sales", "joined_on": "2026-01-01"},
    ])

    # Exact filter for Engineering
    res = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Engineering"

    # Non-existent department filter returns empty items list
    res_none = client.get("/analytics/departments/summary?month=2026-07&department=NonExistent")
    assert res_none.status_code == 200
    assert res_none.json()["items"] == []


# --------------------------------------------------------------------------- #
# 3. GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def test_late_leaderboard_competition_ranking_and_cutoff_ties(client, mock_db):
    # Setup employees:
    mock_db.employees.insert_many([
        {"emp_code": "EMP0001", "name": "Alice", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "Bob", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0003", "name": "Charlie", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0004", "name": "David", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0005", "name": "Eve", "department": "Engineering", "joined_on": "2026-01-01"},
    ])

    # Late minutes in July 2026:
    # Alice (EMP0001): 60 late minutes
    # Bob (EMP0002): 40 late minutes
    # Charlie (EMP0003): 40 late minutes (tied with Bob)
    # David (EMP0004): 20 late minutes
    # Eve (EMP0005): 0 late minutes (must be omitted)
    mock_db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "late_minutes": 60, "status": "PRESENT"},
        {"emp_code": "EMP0002", "date": "2026-07-06", "late_minutes": 40, "status": "PRESENT"},
        {"emp_code": "EMP0003", "date": "2026-07-06", "late_minutes": 40, "status": "PRESENT"},
        {"emp_code": "EMP0004", "date": "2026-07-06", "late_minutes": 20, "status": "PRESENT"},
        {"emp_code": "EMP0005", "date": "2026-07-06", "late_minutes": 0, "status": "PRESENT"},
    ])

    # Request with limit=10:
    # Ranks should be:
    # 1: Alice (60)
    # 2: Bob (40, EMP0002)
    # 2: Charlie (40, EMP0003)
    # 4: David (20)
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 4
    assert items[0] == {
        "rank": 1,
        "emp_code": "EMP0001",
        "name": "Alice",
        "department": "Engineering",
        "total_late_minutes": 60,
        "late_count": 1,
    }
    assert items[1]["rank"] == 2
    assert items[1]["emp_code"] == "EMP0002"
    assert items[2]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0003"
    assert items[3]["rank"] == 4
    assert items[3]["emp_code"] == "EMP0004"

    # CRITICAL: Limit applied AFTER ranking, returning all tied employees at cutoff!
    # Request with limit=2:
    # Ranks <= 2 includes rank 1 (Alice) and both rank 2s (Bob and Charlie).
    # Total rows returned: 3 (exceeds limit=2 due to tie at rank 2).
    res_cutoff = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res_cutoff.status_code == 200
    cutoff_items = res_cutoff.json()["items"]
    assert len(cutoff_items) == 3
    assert [item["emp_code"] for item in cutoff_items] == ["EMP0001", "EMP0002", "EMP0003"]


def test_late_leaderboard_validation_and_department_filter(client, mock_db):
    mock_db.employees.insert_many([
        {"emp_code": "EMP0001", "name": "Eng", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "Sales", "department": "Sales", "joined_on": "2026-01-01"},
    ])
    mock_db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "late_minutes": 30, "status": "PRESENT"},
        {"emp_code": "EMP0002", "date": "2026-07-06", "late_minutes": 45, "status": "PRESENT"},
    ])

    # Validation: limit range 1..50
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422

    # Department filter: Sales only
    res_dept = client.get("/analytics/leaderboard/late?month=2026-07&department=Sales")
    assert res_dept.status_code == 200
    items = res_dept.json()["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0002"
    assert items[0]["rank"] == 1


# --------------------------------------------------------------------------- #
# 4. GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
def test_department_trend_unknown_department_404(client):
    res = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 404
    assert res.json() == {"detail": "Unknown department"}


def test_department_trend_date_validation_422(client, mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })

    # Invalid dates
    assert client.get("/analytics/departments/Engineering/trend?from=2026-02-30&to=2026-03-05").status_code == 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-01").status_code == 422

    # Exactly 92 days boundary: inclusive (d_to - d_from).days + 1 <= 92
    # 2026-01-01 to 2026-04-02: (April 2 - Jan 1) = 31 (Jan) + 28 (Feb) + 31 (Mar) + 1 = 91 days difference + 1 = 92 days
    d1 = date(2026, 1, 1)
    d92 = d1 + timedelta(days=91)  # 92 inclusive calendar days
    res_92 = client.get(f"/analytics/departments/Engineering/trend?from={d1.isoformat()}&to={d92.isoformat()}")
    assert res_92.status_code == 200

    # 93 days exceeds limit -> 422
    d93 = d1 + timedelta(days=92)
    res_93 = client.get(f"/analytics/departments/Engineering/trend?from={d1.isoformat()}&to={d93.isoformat()}")
    assert res_93.status_code == 422
    assert res_93.json() == {"detail": "Date range must not exceed 92 days"}


def test_department_trend_daily_metrics_and_moving_average(client, mock_db):
    # Two employees:
    # EMP0001 joined 2026-07-01
    # EMP0002 joined 2026-07-03
    mock_db.employees.insert_many([
        {"emp_code": "EMP0001", "name": "Asha", "department": "Engineering", "joined_on": "2026-07-01"},
        {"emp_code": "EMP0002", "name": "Bob", "department": "Engineering", "joined_on": "2026-07-03"},
    ])

    # Date range: 2026-07-01 (Wed) to 2026-07-06 (Mon) = 6 days.
    # Wed 2026-07-01: Headcount=1. EMP0001 PRESENT. rate = 1.0 / 1 = 1.0. moving_avg_7d = 1.0.
    # Thu 2026-07-02: Headcount=1. No attendance logs! rate = 0.0 / 1 = 0.0. moving_avg_7d = (1.0 + 0.0) / 2 = 0.5.
    # Fri 2026-07-03: Headcount=2 (Bob joined). EMP0001 PRESENT (half_day=True -> 0.5), Bob PRESENT (1.0).
    #                 present_count = 1.5. rate = 1.5 / 2 = 0.75.
    #                 moving_avg_7d = (1.0 + 0.0 + 0.75) / 3 = 0.5833.
    # Sat 2026-07-04: Weekend! is_working_day = False. rate = None.
    #                 moving_avg_7d = (1.0 + 0.0 + 0.75) / 3 = 0.5833 (ignores Saturday's None).
    # Sun 2026-07-05: Weekend! is_working_day = False. rate = None. moving_avg_7d = 0.5833.
    # Mon 2026-07-06: Headcount=2. Both PRESENT (1.0 + 1.0 = 2.0). rate = 2.0 / 2 = 1.0.
    #                 moving_avg_7d = (1.0 + 0.0 + 0.75 + 1.0) / 4 = 2.75 / 4 = 0.6875.
    mock_db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-03", "status": "PRESENT", "half_day": True, "late_minutes": 15},
        {"emp_code": "EMP0002", "date": "2026-07-03", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0002", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0},
    ])

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-06")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    assert len(items) == 6

    # 2026-07-01 (Wed)
    assert items[0]["date"] == "2026-07-01"
    assert items[0]["is_working_day"] is True
    assert items[0]["headcount"] == 1
    assert items[0]["present_count"] == 1.0
    assert items[0]["attendance_rate"] == 1.0
    assert items[0]["moving_avg_7d"] == 1.0

    # 2026-07-02 (Thu) - Gap day filled
    assert items[1]["date"] == "2026-07-02"
    assert items[1]["is_working_day"] is True
    assert items[1]["headcount"] == 1
    assert items[1]["present_count"] == 0.0
    assert items[1]["attendance_rate"] == 0.0
    assert items[1]["moving_avg_7d"] == 0.5

    # 2026-07-03 (Fri)
    assert items[2]["date"] == "2026-07-03"
    assert items[2]["is_working_day"] is True
    assert items[2]["headcount"] == 2
    assert items[2]["present_count"] == 1.5
    assert items[2]["late_count"] == 1
    assert items[2]["attendance_rate"] == 0.75
    assert items[2]["moving_avg_7d"] == 0.5833

    # 2026-07-04 (Sat)
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["is_working_day"] is False
    assert items[3]["headcount"] == 2
    assert items[3]["attendance_rate"] is None
    assert items[3]["moving_avg_7d"] == 0.5833

    # 2026-07-05 (Sun)
    assert items[4]["date"] == "2026-07-05"
    assert items[4]["is_working_day"] is False
    assert items[4]["headcount"] == 2
    assert items[4]["attendance_rate"] is None
    assert items[4]["moving_avg_7d"] == 0.5833

    # 2026-07-06 (Mon)
    assert items[5]["date"] == "2026-07-06"
    assert items[5]["is_working_day"] is True
    assert items[5]["headcount"] == 2
    assert items[5]["present_count"] == 2.0
    assert items[5]["attendance_rate"] == 1.0
    assert items[5]["moving_avg_7d"] == 0.6875


def test_employee_monthly_legacy_missing_fields(client, mock_db):
    # Legacy record in attendance_logs omitting half_day, late_minutes, overtime_minutes
    mock_db.employees.insert_one({
        "emp_code": "EMP0010",
        "name": "Legacy Emp",
        "email": "legacy@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    # Omitted half_day -> defaults to false (1.0 present day); omitted late/overtime -> 0
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0010",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 8.0,
    })

    res = client.get("/analytics/employees/EMP0010/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["present_days"] == 1.0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0


def test_department_summary_zero_qualifying_work_hour_records(client, mock_db):
    # Department where records have presence status, but work_hours is null (unclosed punch-ins)
    mock_db.employees.insert_one({
        "emp_code": "EMP0020",
        "name": "No Hours",
        "department": "Security",
        "joined_on": "2026-01-01",
    })
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0020",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": None,
        "late_minutes": 0,
    })

    res = client.get("/analytics/departments/summary?month=2026-07&department=Security")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    sec = items[0]
    assert sec["headcount"] == 1
    assert sec["present_days"] == 1.0
    # Must be null, not 0.0 or error
    assert sec["avg_work_hours"] is None


def test_department_trend_no_logs_and_weekend_start(client, mock_db):
    # Department with employees, but requested range starts on Saturday and has ZERO logs
    mock_db.employees.insert_one({
        "emp_code": "EMP0030",
        "name": "Weekend Start",
        "department": "Operations",
        "joined_on": "2026-01-01",
    })

    # Range: 2026-07-04 (Sat) to 2026-07-06 (Mon)
    res = client.get("/analytics/departments/Operations/trend?from=2026-07-04&to=2026-07-06")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3

    # Saturday: weekend, attendance_rate=None, moving_avg_7d=None
    assert items[0]["date"] == "2026-07-04"
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    # Sunday: weekend, attendance_rate=None, moving_avg_7d=None
    assert items[1]["date"] == "2026-07-05"
    assert items[1]["is_working_day"] is False
    assert items[1]["attendance_rate"] is None
    assert items[1]["moving_avg_7d"] is None

    # Monday: working day with 0 logs -> rate=0.0, moving_avg_7d=0.0
    assert items[2]["date"] == "2026-07-06"
    assert items[2]["is_working_day"] is True
    assert items[2]["headcount"] == 1
    assert items[2]["present_count"] == 0.0
    assert items[2]["attendance_rate"] == 0.0
    assert items[2]["moving_avg_7d"] == 0.0


def test_strict_production_mode_does_not_swallow_errors(client, mock_db, monkeypatch):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Prod Emp",
        "email": "prod@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    # Simulate production mode where is_mock_db() returns False
    monkeypatch.setattr(app_module, "is_mock_db", lambda: False)

    # In production mode, an operation error on attendance_logs must raise, NOT fall back silently
    def failing_aggregate(*args, **kwargs):
        raise pymongo.errors.OperationFailure("Simulated production database error")

    monkeypatch.setattr(mock_db.attendance_logs, "aggregate", failing_aggregate)

    with pytest.raises(pymongo.errors.OperationFailure):
        client.get("/analytics/employees/EMP0001/monthly?month=2026-07")

