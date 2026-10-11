import os
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
import pymongo
import pytest
from fastapi.testclient import TestClient

import app.main as app_module
from app.main import app

# Load local environment configuration
load_dotenv()
REAL_MONGO_URI = os.getenv("MONGO_URI")
REAL_MONGO_DB = os.getenv("MONGO_DB")


def check_real_mongo_preconditions():
    """Verify safe preconditions for executing real MongoDB integration tests."""
    if not REAL_MONGO_URI:
        pytest.skip("MONGO_URI is not configured in local environment")
    if not REAL_MONGO_DB:
        pytest.skip("MONGO_DB is not configured in local environment")
    if REAL_MONGO_DB.lower() in ("admin", "config", "local"):
        pytest.fail(f"Safety violation: integration tests must not target system database '{REAL_MONGO_DB}'")
    try:
        client = pymongo.MongoClient(REAL_MONGO_URI, serverSelectionTimeoutMS=2000)
        info = client.server_info()
        ver_str = info.get("version", "0.0.0")
        major_minor = tuple(map(int, ver_str.split(".")[:2]))
        if major_minor < (6, 0):
            pytest.fail(f"Real MongoDB version must be >= 6.0, found {ver_str}")
        return client
    except Exception as e:
        pytest.fail(f"Failed to connect to real MongoDB instance: {e}")


@pytest.fixture(scope="session")
def real_mongo_client():
    client = check_real_mongo_preconditions()
    yield client
    client.close()


@pytest.fixture
def real_db(real_mongo_client):
    db = real_mongo_client[REAL_MONGO_DB]
    assert db.name == REAL_MONGO_DB, f"Safety check: active db must match configured '{REAL_MONGO_DB}'"

    # Clean collections before test
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    # Bind application to real MongoDB
    orig_client = app_module.client
    orig_db = app_module.db
    app_module.client = real_mongo_client
    app_module.db = db

    # Ensure production indexes are created on real MongoDB
    app_module.ensure_indexes()

    yield db

    # Clean collections after test
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    app_module.client = orig_client
    app_module.db = orig_db


@pytest.fixture
def client(real_db):
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. Real MongoDB Server Version & Index Verification
# --------------------------------------------------------------------------- #
def test_real_mongo_server_version_and_indexes(real_mongo_client, real_db):
    info = real_mongo_client.server_info()
    version = info.get("version", "0.0.0")
    major_minor = tuple(map(int, version.split(".")[:2]))
    assert major_minor >= (6, 0), f"Expected MongoDB >= 6.0, got {version}"

    # Verify employees indexes on real MongoDB
    emp_indexes = real_db.employees.index_information()
    assert "uniq_emp_code" in emp_indexes
    assert emp_indexes["uniq_emp_code"]["unique"] is True
    assert emp_indexes["uniq_emp_code"]["key"] == [("emp_code", 1)]
    assert "idx_dept_joined" in emp_indexes
    assert emp_indexes["idx_dept_joined"]["key"] == [("department", 1), ("joined_on", 1)]
    assert "idx_joined_on" in emp_indexes
    assert emp_indexes["idx_joined_on"]["key"] == [("joined_on", 1)]


    # Verify attendance_logs indexes on real MongoDB
    att_indexes = real_db.attendance_logs.index_information()
    assert "uniq_emp_date" in att_indexes
    assert att_indexes["uniq_emp_date"]["unique"] is True
    assert att_indexes["uniq_emp_date"]["key"] == [("emp_code", 1), ("date", 1)]
    assert "idx_date_emp" in att_indexes
    assert att_indexes["idx_date_emp"]["key"] == [("date", -1), ("emp_code", 1)]
    assert "idx_emp_date_status" in att_indexes
    assert att_indexes["idx_emp_date_status"]["key"] == [("emp_code", 1), ("date", 1), ("status", 1)]
    assert "idx_date_status" in att_indexes
    assert att_indexes["idx_date_status"]["key"] == [("date", 1), ("status", 1)]
    assert "idx_date_late_minutes" in att_indexes
    assert att_indexes["idx_date_late_minutes"]["key"] == [("date", 1), ("late_minutes", 1)]
    assert "idx_emp_punch_in" in att_indexes
    assert att_indexes["idx_emp_punch_in"]["key"] == [("emp_code", 1), ("punch_in", -1)]


# --------------------------------------------------------------------------- #
# 2. Employee APIs on Real MongoDB
# --------------------------------------------------------------------------- #
def test_real_mongo_create_employee_and_unique_constraint(client):
    payload = {
        "emp_code": "EMP0101",
        "name": "Integration User",
        "email": "integration@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01",
    }
    # Successful creation
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0101"
    assert isinstance(data["created_at"], int)

    # Duplicate creation rejected by real MongoDB unique index
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409
    assert res_dup.json() == {"detail": "emp_code already exists"}


def test_real_mongo_list_employees_filtering_and_pagination(client, real_db):
    for i in range(1, 6):
        real_db.employees.insert_one({
            "emp_code": f"EMP010{i}",
            "name": f"User {i}",
            "email": f"user{i}@example.com",
            "department": "Engineering" if i <= 3 else "Product",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-06-01",
            "created_at": datetime.now(timezone.utc),
        })

    # Filter Engineering
    res = client.get("/employees?department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert [e["emp_code"] for e in data["items"]] == ["EMP0101", "EMP0102", "EMP0103"]

    # Pagination page 2, size 2 on all employees
    res_p2 = client.get("/employees?page=2&page_size=2")
    assert res_p2.status_code == 200
    p2_data = res_p2.json()
    assert p2_data["total"] == 5
    assert len(p2_data["items"]) == 2
    assert [e["emp_code"] for e in p2_data["items"]] == ["EMP0103", "EMP0104"]


# --------------------------------------------------------------------------- #
# 3. Attendance APIs on Real MongoDB
# --------------------------------------------------------------------------- #
def test_real_mongo_punch_in_overnight_late_and_duplicate(client, real_db):
    # Setup overnight shift employee (22:00 to 06:00)
    real_db.employees.insert_one({
        "emp_code": "EMP0201",
        "name": "Night Guard",
        "email": "guard@example.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    # Punch in on 2026-07-02 at 01:15 IST (overnight shift belonging to 2026-07-01 IST date)
    # 2026-07-02 01:15 IST = 2026-07-01 19:45:00 UTC
    dt_utc = datetime(2026, 7, 1, 19, 45, 0, tzinfo=timezone.utc)
    ts_ms = int(dt_utc.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0201", "punched_at": ts_ms})
    assert res.status_code == 201
    log = res.json()
    assert log["date"] == "2026-07-01"  # R1 overnight shift assigned to start date
    assert log["status"] == "PRESENT"

    # Second punch in for same employee on same date -> 409 from real unique index
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0201", "punched_at": ts_ms})
    assert res_dup.status_code == 409
    assert res_dup.json() == {"detail": "Already punched in for that date"}


def test_real_mongo_punch_out_and_work_hours(client, real_db):
    real_db.employees.insert_one({
        "emp_code": "EMP0202",
        "name": "Day Engineer",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    # Punch-in at 09:30 IST (04:00 UTC)
    in_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0202", "punched_at": int(in_utc.timestamp() * 1000)})
    assert res_in.status_code == 201

    # Punch-out at 19:15 IST (13:45 UTC): 9.75 work hours, 45 mins overtime
    out_utc = datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0202", "punched_at": int(out_utc.timestamp() * 1000)})
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["work_hours"] == 9.75
    assert out_data["overtime_minutes"] == 45
    assert out_data["half_day"] is False

    # Second punch out -> 409
    res_out2 = client.post("/attendance/punch-out", json={"emp_code": "EMP0202", "punched_at": int(out_utc.timestamp() * 1000)})
    assert res_out2.status_code == 409


def test_real_mongo_regularization_audit_history(client, real_db):
    real_db.employees.insert_one({
        "emp_code": "EMP0203",
        "name": "Audit Staff",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    in_utc = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0203", "punched_at": int(in_utc.timestamp() * 1000)})
    assert res_in.status_code == 201

    # Regularize status to WFH
    reg_res = client.patch("/attendance/EMP0203/2026-07-07", json={
        "status": "WFH",
        "reason": "WFH approved by manager",
        "regularized_by": "MGR001",
    })
    assert reg_res.status_code == 200
    reg_data = reg_res.json()
    assert reg_data["status"] == "WFH"
    assert len(reg_data["history"]) == 1
    assert reg_data["history"][0]["by"] == "MGR001"
    assert reg_data["history"][0]["changes"]["status"]["to"] == "WFH"


# --------------------------------------------------------------------------- #
# 4. Production Analytics Pipelines on Real MongoDB 6.0
# --------------------------------------------------------------------------- #
def test_real_mongo_pipeline_employee_monthly(client, real_db):
    # Setup employee who joined mid-month on 2026-07-15 (Wed)
    real_db.employees.insert_one({
        "emp_code": "EMP0301",
        "name": "Mid Month Joiner",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
    })

    # Add 1 full day (2026-07-15), 1 half day (2026-07-16), 1 weekend log (2026-07-18)
    real_db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0301",
            "date": "2026-07-15",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP0301",
            "date": "2026-07-16",
            "status": "PRESENT",
            "work_hours": 4.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
        },
        {
            "emp_code": "EMP0301",
            "date": "2026-07-18",  # Saturday
            "status": "PRESENT",
            "work_hours": 5.0,
            "late_minutes": 10,
            "overtime_minutes": 60,
            "half_day": False,
        },
    ])

    res = client.get("/analytics/employees/EMP0301/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0301"
    assert data["month"] == "2026-07"
    # Working days from 2026-07-15 to 2026-07-31: 13 days
    assert data["working_days"] == 13
    # Present days: 1.0 (weekday full) + 0.5 (weekday half) + 0.0 (weekend) = 1.5
    assert data["present_days"] == 1.5
    # Late count: 2 (weekday late + weekend late)
    assert data["late_count"] == 2
    assert data["total_late_minutes"] == 25
    # Overtime minutes: 60 (from weekend log)
    assert data["total_overtime_minutes"] == 60
    # Attendance percentage: (1.5 / 13) * 100 = 11.538... -> 11.54%
    assert data["attendance_pct"] == 11.54


def test_real_mongo_pipeline_department_summary(client, real_db):
    # Setup 2 employees in Sales, 1 joins before month end, 1 joins after
    real_db.employees.insert_many([
        {
            "emp_code": "EMP0401",
            "name": "Sales Active",
            "department": "Sales",
            "joined_on": "2026-07-01",
        },
        {
            "emp_code": "EMP0402",
            "name": "Sales Zero Logs",
            "department": "Sales",
            "joined_on": "2026-07-10",
        },
        {
            "emp_code": "EMP0403",
            "name": "Sales Future",
            "department": "Sales",
            "joined_on": "2026-08-01",  # Excluded!
        },
    ])

    # Log for EMP0401 only
    real_db.attendance_logs.insert_one({
        "emp_code": "EMP0401",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 20,
        "half_day": False,
    })

    res = client.get("/analytics/departments/summary?month=2026-07&department=Sales")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    sales = items[0]
    assert sales["department"] == "Sales"
    # Headcount: EMP0401 and EMP0402 (EMP0403 excluded as joined in August)
    assert sales["headcount"] == 2
    assert sales["present_days"] == 1.0
    assert sales["avg_work_hours"] == 8.0
    assert sales["late_count"] == 1
    assert sales["total_late_minutes"] == 20


def test_real_mongo_pipeline_late_leaderboard_competition_ranking(client, real_db):
    real_db.employees.insert_many([
        {"emp_code": "EMP0501", "name": "Emp One", "department": "Tech", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0502", "name": "Emp Two", "department": "Tech", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0503", "name": "Emp Three", "department": "Tech", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0504", "name": "Emp Four", "department": "Tech", "joined_on": "2026-01-01"},
    ])

    # Late minutes:
    # EMP0501: 60 mins
    # EMP0502: 40 mins (tie rank 2)
    # EMP0503: 40 mins (tie rank 2)
    # EMP0504: 20 mins (rank 4)
    real_db.attendance_logs.insert_many([
        {"emp_code": "EMP0501", "date": "2026-07-06", "late_minutes": 60, "status": "PRESENT"},
        {"emp_code": "EMP0502", "date": "2026-07-06", "late_minutes": 40, "status": "PRESENT"},
        {"emp_code": "EMP0503", "date": "2026-07-06", "late_minutes": 40, "status": "PRESENT"},
        {"emp_code": "EMP0504", "date": "2026-07-06", "late_minutes": 20, "status": "PRESENT"},
    ])

    # Request limit=2: Ranks 1, 2, 2 must all be included (cutoff tie returns 3 rows!)
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0501"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 60

    # Tied ranks
    assert items[1]["emp_code"] == "EMP0502"
    assert items[1]["rank"] == 2
    assert items[1]["total_late_minutes"] == 40

    assert items[2]["emp_code"] == "EMP0503"
    assert items[2]["rank"] == 2
    assert items[2]["total_late_minutes"] == 40


def test_real_mongo_pipeline_department_trend_complete_window(client, real_db):
    # Setup employees in Support
    # EMP0601 joined 2026-07-01
    # EMP0602 joined 2026-07-03
    real_db.employees.insert_many([
        {"emp_code": "EMP0601", "name": "Sup One", "department": "Support", "joined_on": "2026-07-01"},
        {"emp_code": "EMP0602", "name": "Sup Two", "department": "Support", "joined_on": "2026-07-03"},
    ])

    # 2026-07-01 (Wed): EMP0601 PRESENT (1.0)
    # 2026-07-02 (Thu): 0 logs
    # 2026-07-03 (Fri): EMP0601 PRESENT, EMP0602 PRESENT (2.0)
    # 2026-07-04 (Sat): weekend, 0 logs
    # 2026-07-05 (Sun): weekend, 0 logs
    real_db.attendance_logs.insert_many([
        {"emp_code": "EMP0601", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP0601", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 10, "half_day": False},
        {"emp_code": "EMP0602", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 0, "half_day": False},
    ])

    res = client.get("/analytics/departments/Support/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5

    # Day 0: 2026-07-01 (Wed) - Headcount=1, present=1.0, rate=1.0, MA=1.0
    assert items[0]["date"] == "2026-07-01"
    assert items[0]["is_working_day"] is True
    assert items[0]["headcount"] == 1
    assert items[0]["present_count"] == 1.0
    assert items[0]["attendance_rate"] == 1.0
    assert items[0]["moving_avg_7d"] == 1.0

    # Day 1: 2026-07-02 (Thu) - Headcount=1, present=0.0, rate=0.0, MA=0.5
    assert items[1]["date"] == "2026-07-02"
    assert items[1]["is_working_day"] is True
    assert items[1]["headcount"] == 1
    assert items[1]["present_count"] == 0.0
    assert items[1]["attendance_rate"] == 0.0
    assert items[1]["moving_avg_7d"] == 0.5

    # Day 2: 2026-07-03 (Fri) - Headcount=2, present=2.0, rate=1.0, MA=(1+0+1)/3 = 0.6667
    assert items[2]["date"] == "2026-07-03"
    assert items[2]["is_working_day"] is True
    assert items[2]["headcount"] == 2
    assert items[2]["present_count"] == 2.0
    assert items[2]["attendance_rate"] == 1.0
    assert items[2]["moving_avg_7d"] == 0.6667

    # Day 3: 2026-07-04 (Sat) - Weekend, rate=None, MA=(1+0+1)/3 = 0.6667
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["is_working_day"] is False
    assert items[3]["attendance_rate"] is None
    assert items[3]["moving_avg_7d"] == 0.6667

    # Day 4: 2026-07-05 (Sun) - Weekend, rate=None, MA=(1+0+1)/3 = 0.6667
    assert items[4]["date"] == "2026-07-05"
    assert items[4]["is_working_day"] is False
    assert items[4]["attendance_rate"] is None
    assert items[4]["moving_avg_7d"] == 0.6667


def test_real_mongo_pipeline_employee_monthly_zero_logs_and_future_joiner(client, real_db):
    # Setup zero-logs employee and future joiner
    real_db.employees.insert_many([
        {
            "emp_code": "EMP0701",
            "name": "Zero Logs Emp",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
        {
            "emp_code": "EMP0702",
            "name": "Future Joiner Emp",
            "department": "Engineering",
            "joined_on": "2026-08-15",
        },
    ])

    # 1. Zero logs employee in July 2026
    res1 = client.get("/analytics/employees/EMP0701/monthly?month=2026-07")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["working_days"] == 23  # Full July 2026 weekdays
    assert d1["present_days"] == 0.0
    assert d1["attendance_pct"] == 0.0
    assert d1["late_count"] == 0

    # 2. Future joiner in July 2026 (joined in August)
    res2 = client.get("/analytics/employees/EMP0702/monthly?month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["working_days"] == 0
    assert d2["present_days"] == 0.0
    assert d2["attendance_pct"] is None


def test_real_mongo_pipeline_department_trend_empty_range_and_weekend_start(client, real_db):
    # Department exists, but has ZERO attendance logs for the requested range
    real_db.employees.insert_one({
        "emp_code": "EMP0801",
        "name": "Operations Emp",
        "department": "Operations",
        "joined_on": "2026-01-01",
    })

    # Range: 2026-07-04 (Sat) to 2026-07-06 (Mon)
    res = client.get("/analytics/departments/Operations/trend?from=2026-07-04&to=2026-07-06")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3

    # Saturday: weekend -> rate=None, MA=None
    assert items[0]["date"] == "2026-07-04"
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    # Sunday: weekend -> rate=None, MA=None
    assert items[1]["date"] == "2026-07-05"
    assert items[1]["is_working_day"] is False
    assert items[1]["attendance_rate"] is None
    assert items[1]["moving_avg_7d"] is None

    # Monday: working day with 0 logs -> rate=0.0, MA=0.0
    assert items[2]["date"] == "2026-07-06"
    assert items[2]["is_working_day"] is True
    assert items[2]["headcount"] == 1
    assert items[2]["present_count"] == 0.0
    assert items[2]["attendance_rate"] == 0.0
    assert items[2]["moving_avg_7d"] == 0.0


def test_real_mongo_pipeline_department_summary_zero_qualifying_work_hours(client, real_db):
    real_db.employees.insert_one({
        "emp_code": "EMP0901",
        "name": "Unclosed Shift Staff",
        "department": "Security",
        "joined_on": "2026-01-01",
    })

    # Record with status PRESENT, but work_hours is null (unclosed punch-in)
    real_db.attendance_logs.insert_one({
        "emp_code": "EMP0901",
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
    assert sec["avg_work_hours"] is None


def test_real_mongo_list_attendance_date_boundaries_and_sorting(client, real_db):
    for i in range(1, 4):
        real_db.attendance_logs.insert_one({
            "emp_code": f"EMP000{i}",
            "date": f"2026-07-0{i}",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, i, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, i, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 30,
            "half_day": False,
        })

    # Verify compound sort: date desc, emp_code asc
    res = client.get("/attendance?date_from=2026-07-01&date_to=2026-07-03")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["date"] == "2026-07-03"
    assert items[1]["date"] == "2026-07-02"
    assert items[2]["date"] == "2026-07-01"


# --------------------------------------------------------------------------- #
# 5. Real MongoDB /admin/explain/{endpoint} Verification
# --------------------------------------------------------------------------- #
def test_real_mongo_admin_explain_all_five_endpoints(client, real_db):
    # Seed representative records so query planner evaluates indexes
    real_db.employees.insert_many([
        {
            "emp_code": f"EXP{i:04d}",
            "name": f"Explain User {i}",
            "department": "Engineering" if i % 2 == 0 else "Sales",
            "joined_on": "2026-01-01",
        }
        for i in range(50)
    ])
    real_db.attendance_logs.insert_many([
        {
            "emp_code": f"EXP{i:04d}",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 15,
            "work_hours": 8.0,
            "half_day": False,
        }
        for i in range(50)
    ])

    def has_stage(plan, target_stage):
        if isinstance(plan, dict):
            if plan.get("stage") == target_stage:
                return True
            return any(has_stage(v, target_stage) for v in plan.values())
        elif isinstance(plan, list):
            return any(has_stage(item, target_stage) for item in plan)
        return False

    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?emp_code=EXP0001&date_from=2026-07-01&date_to=2026-07-31")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["endpoint"] == "attendance_list"
    assert d1["collection"] == "attendance_logs"
    assert "executionStats" in d1["explain"]
    assert has_stage(d1["explain"], "IXSCAN")
    assert not has_stage(d1["explain"].get("queryPlanner", {}).get("winningPlan", {}), "COLLSCAN")

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EXP0001&month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["endpoint"] == "employee_monthly"
    assert d2["collection"] == "attendance_logs"
    assert "stages" in d2["explain"]
    cursor_stage2 = d2["explain"]["stages"][0].get("$cursor", {})
    assert has_stage(cursor_stage2, "IXSCAN")
    assert not has_stage(cursor_stage2.get("queryPlanner", {}).get("winningPlan", {}), "COLLSCAN")

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["endpoint"] == "department_summary"
    assert d3["collection"] == "employees"
    assert "stages" in d3["explain"]
    cursor_stage3 = d3["explain"]["stages"][0].get("$cursor", {})
    assert has_stage(cursor_stage3, "IXSCAN")
    assert not has_stage(cursor_stage3.get("queryPlanner", {}).get("winningPlan", {}), "COLLSCAN")

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10&department=Engineering")
    assert res4.status_code == 200
    d4 = res4.json()
    assert d4["endpoint"] == "late_leaderboard"
    assert d4["collection"] == "attendance_logs"
    assert "stages" in d4["explain"]
    cursor_stage4 = d4["explain"]["stages"][0].get("$cursor", {})
    assert has_stage(cursor_stage4, "IXSCAN")
    assert not has_stage(cursor_stage4.get("queryPlanner", {}).get("winningPlan", {}), "COLLSCAN")

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
    assert res5.status_code == 200
    d5 = res5.json()
    assert d5["endpoint"] == "department_trend"
    assert d5["collection"] == "employees"
    assert "stages" in d5["explain"]
    cursor_stage5 = d5["explain"]["stages"][0].get("$cursor", {})
    assert has_stage(cursor_stage5, "IXSCAN")
    assert not has_stage(cursor_stage5.get("queryPlanner", {}).get("winningPlan", {}), "COLLSCAN")


# --------------------------------------------------------------------------- #
# 16. Real MongoDB Legacy Regularization & Concurrency Tests
# --------------------------------------------------------------------------- #
def test_real_mongo_regularize_legacy_missing_history_and_concurrency(real_db, client):
    real_db.employees.insert_one({
        "emp_code": "LEG_REAL_01",
        "name": "Legacy Real",
        "email": "legreal@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    })
    real_db.attendance_logs.insert_one({
        "emp_code": "LEG_REAL_01",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc),
        "work_hours": 8.98,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })

    new_in_ms = int(datetime(2026, 7, 14, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    resp = client.patch(
        "/attendance/LEG_REAL_01/2026-07-14",
        json={
            "regularized_by": "hr.admin",
            "reason": "sync punch",
            "punch_in": new_in_ms,
        },
    )
    assert resp.status_code == 200
    doc = resp.json()
    assert len(doc["history"]) == 1
    assert "half_day" not in doc["history"][0]["changes"]
    assert "punch_in" in doc["history"][0]["changes"]

    # Test race condition on same legacy record
    from concurrent.futures import ThreadPoolExecutor
    def attempt_concurrent(i):
        test_cli = TestClient(app)
        return test_cli.patch(
            "/attendance/LEG_REAL_01/2026-07-14",
            json={
                "regularized_by": f"admin_{i}",
                "reason": f"concurrent update {i}",
                "punch_in": 1783999800000 + (i * 60000),
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = list(executor.map(attempt_concurrent, range(1, 5)))

    assert 200 in statuses
    assert all(code in (200, 409) for code in statuses)


# --------------------------------------------------------------------------- #
# 17. Real MongoDB Production Trend Midpoint Half-Up Rounding Verification
# --------------------------------------------------------------------------- #
def test_real_mongo_trend_exact_midpoint_half_up_rounding(real_db, client):
    real_db.employees.delete_many({"department": "MidpointDept"})
    real_db.attendance_logs.delete_many({"date": "2026-07-06"})

    # Create 20,000 employees so that 7 present gives 7 / 20000 = 0.00035
    # (Exact half-up midpoint: 0.00035 must round to 0.0004, whereas banker's rounding would give 0.0003)
    emps = []
    for i in range(1, 20001):
        emps.append({
            "emp_code": f"MP_{i:05d}",
            "name": f"Midpoint Emp {i}",
            "email": f"mp{i}@example.com",
            "department": "MidpointDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
            "created_at": datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc),
        })
    real_db.employees.insert_many(emps)

    # Insert exactly 7 present attendance logs on Monday 2026-07-06
    logs = []
    for i in range(1, 8):
        logs.append({
            "emp_code": f"MP_{i:05d}",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })
    real_db.attendance_logs.insert_many(logs)

    resp = client.get("/analytics/departments/MidpointDept/trend?from=2026-07-06&to=2026-07-06")
    assert resp.status_code == 200
    data = resp.json()
    item = data["items"][0]
    assert item["headcount"] == 20000
    assert item["present_count"] == 7.0
    # Strict Rule R8: 0.00035 rounds half-up to 0.0004
    assert item["attendance_rate"] == 0.0004
    assert item["moving_avg_7d"] == 0.0004



