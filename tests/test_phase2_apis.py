from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import mongomock
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import (
    app,
    get_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    truncate_to_seconds,
    ms_to_utc_dt,
    dt_to_epoch_ms,
    IST,
    UTC,
)


@pytest.fixture
def mock_db():
    client = mongomock.MongoClient()
    db = client["test_attendance_db"]
    # Seed unique indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    with patch("app.main.db", db), patch("app.main.client", client):
        yield db


@pytest.fixture
def client(mock_db):
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Unit Tests: Helpers and Rules (R1 - R5)
# --------------------------------------------------------------------------- #
def test_r1_overnight_shift_date():
    # Overnight shift 22:00 - 06:00
    # Punch in at 05:50 IST on 2026-07-07 -> should belong to 2026-07-06
    ist_dt = datetime(2026, 7, 7, 5, 50, 0, tzinfo=IST)
    utc_dt = ist_dt.astimezone(UTC)
    att_date = get_attendance_date(utc_dt, "22:00", "06:00")
    assert att_date == "2026-07-06"

    # Punch in at 22:05 IST on 2026-07-06 -> belongs to 2026-07-06
    ist_dt2 = datetime(2026, 7, 6, 22, 5, 0, tzinfo=IST)
    utc_dt2 = ist_dt2.astimezone(UTC)
    assert get_attendance_date(utc_dt2, "22:00", "06:00") == "2026-07-06"


def test_r2_late_minutes_grace_boundary():
    # Shift 09:30
    # 09:40:00 -> 0
    t1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(UTC)
    assert compute_late_minutes(t1, "09:30", "2026-07-06") == 0

    # 09:40:01 -> 10
    t2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(UTC)
    assert compute_late_minutes(t2, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    t3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(UTC)
    assert compute_late_minutes(t3, "09:30", "2026-07-06") == 45


def test_r3_overtime_threshold():
    # Shift 09:30 - 18:30
    # Punch out at 18:59 (29 min OT) -> 0
    po1 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST).astimezone(UTC)
    assert compute_overtime(po1, "18:30", "2026-07-06", "09:30") == 0

    # Punch out at 19:00 (30 min OT) -> 30
    po2 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(UTC)
    assert compute_overtime(po2, "18:30", "2026-07-06", "09:30") == 30

    # Overnight shift 22:00 - 06:00
    # Shift end is 06:00 on 2026-07-07. Punch out at 06:40 on 2026-07-07 (40 min OT) -> 40
    po3 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST).astimezone(UTC)
    assert compute_overtime(po3, "06:00", "2026-07-06", "22:00") == 40


def test_r4_work_hours_half_up_rounding():
    # Test half-up rounding: 7650 seconds = 2.125 hours -> half-up rounds to 2.13
    p_in = datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC)
    p_out = p_in + timedelta(seconds=7650)
    wh = compute_work_hours(p_in, p_out)
    assert wh == 2.13


# --------------------------------------------------------------------------- #
# Endpoint Tests: POST & GET /employees
# --------------------------------------------------------------------------- #
def test_create_employee_success_and_schema(client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    resp = client.post("/employees", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == "EMP0001"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000


def test_create_employee_validation_errors(client):
    # Invalid emp_code
    resp = client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Test",
        "email": "test@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    assert resp.status_code == 422

    # Equal shift_start and shift_end
    resp2 = client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Test",
        "email": "test@example.com",
        "department": "Eng",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-01",
    })
    assert resp2.status_code == 422


def test_create_employee_duplicate_409(client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    assert client.post("/employees", json=payload).status_code == 201
    assert client.post("/employees", json=payload).status_code == 409


def test_list_employees_pagination_and_filtering(client):
    for i in range(1, 26):
        client.post("/employees", json={
            "emp_code": f"EMP{i:04d}",
            "name": f"User {i}",
            "email": f"user{i}@example.com",
            "department": "Engineering" if i <= 15 else "Sales",
            "joined_on": "2026-01-01",
        })

    # Page 1 (default page_size=20)
    p1 = client.get("/employees").json()
    assert p1["total"] == 25
    assert len(p1["items"]) == 20
    assert p1["items"][0]["emp_code"] == "EMP0001"

    # Page 2
    p2 = client.get("/employees?page=2").json()
    assert len(p2["items"]) == 5
    assert p2["items"][0]["emp_code"] == "EMP0021"

    # Department filter
    dept_eng = client.get("/employees?department=Engineering").json()
    assert dept_eng["total"] == 15
    assert len(dept_eng["items"]) == 15


# --------------------------------------------------------------------------- #
# Endpoint Tests: POST /attendance/punch-in
# --------------------------------------------------------------------------- #
def test_punch_in_unknown_employee(client):
    resp = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert resp.status_code == 404


def test_punch_in_valid_and_overnight(client):
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Meera",
        "email": "meera@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
    })

    # Punch in 21:55 IST on 2026-07-06 (16:25 UTC)
    ts = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=UTC).timestamp() * 1000)
    resp = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": ts,
        "status": "PRESENT"
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["date"] == "2026-07-06"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert "_id" not in data
    assert "id" not in data


def test_punch_in_duplicate_date_409(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    ts1 = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts1}).status_code == 201
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts1}).status_code == 409


def test_punch_in_invalid_timestamp_types(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    # String timestamp rejected
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"}).status_code == 422
    # Seconds-looking rejected (< 100000000000)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500}).status_code == 422
    # Boolean rejected
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": True}).status_code == 422


# --------------------------------------------------------------------------- #
# Endpoint Tests: POST /attendance/punch-out
# --------------------------------------------------------------------------- #
def test_punch_out_success_and_calculations(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    # Punch in 09:28 IST (03:58 UTC) on 2026-07-06
    pin_ts = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})

    # Punch out 18:35 IST (13:05 UTC) on 2026-07-06 (9h 7m = 9.12 hours, 5m OT -> 0)
    pout_ts = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=UTC).timestamp() * 1000)
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout_ts})
    assert resp.status_code == 200
    data = resp.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False


def test_punch_out_half_day_and_overtime(client):
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    # Punch in 10:05 IST (04:35 UTC) -> late 35 min
    pin_ts = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": pin_ts})

    # Punch out 19:10 IST (13:40 UTC) -> 40m OT, 9.08 hours
    pout_ts = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=UTC).timestamp() * 1000)
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": pout_ts})
    assert resp.status_code == 200
    data = resp.json()
    assert data["late_minutes"] == 35
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 9.08
    assert data["half_day"] is False

    # Second punch out attempt on already-closed record yields 409
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": pout_ts + 1000}).status_code == 409


def test_punch_out_duration_boundaries(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    pin_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})

    # Punch out <= punch_in -> 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pin_ts}).status_code == 422
    # Punch out > 24 hours -> 422
    pout_25h = pin_ts + (25 * 3600 * 1000)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout_25h}).status_code == 422


# --------------------------------------------------------------------------- #
# Endpoint Tests: GET /attendance
# --------------------------------------------------------------------------- #
def test_list_attendance_filters_and_validation(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    pin1 = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin1})

    # Date range validation: date_from > date_to -> 422
    resp_val = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert resp_val.status_code == 422

    # Normal list
    resp = client.get("/attendance?date_from=2026-07-01&date_to=2026-07-10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["emp_code"] == "EMP0001"


# --------------------------------------------------------------------------- #
# Endpoint Tests: PATCH /attendance/{emp_code}/{date} (Regularization)
# --------------------------------------------------------------------------- #
def test_regularize_attendance_success_and_history(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    # Punch in 10:05 IST (late 35), punch out 18:30 (work hours 8.42)
    pin_ts = int(datetime(2026, 7, 7, 4, 35, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})
    pout_ts = int(datetime(2026, 7, 7, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout_ts})

    # HR corrects punch_in to 09:28 IST (03:58 UTC) -> late 0, work hours 9.03
    corrected_pin_ts = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=UTC).timestamp() * 1000)
    patch_resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": corrected_pin_ts,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert patch_resp.status_code == 200
    res_data = patch_resp.json()
    assert res_data["late_minutes"] == 0
    assert res_data["work_hours"] == 9.03
    assert len(res_data["history"]) == 1

    h_entry = res_data["history"][0]
    assert h_entry["by"] == "hr.admin"
    assert h_entry["reason"] == "biometric glitch"
    assert "punch_in" in h_entry["changes"]
    assert "late_minutes" in h_entry["changes"]
    assert h_entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h_entry["changes"]["punch_in"]["from"] == pin_ts
    assert h_entry["changes"]["punch_in"]["to"] == corrected_pin_ts


def test_regularize_status_to_absent_clears_times(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    pin_ts = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})

    # Changing to ABSENT while providing punch_in is 422
    bad_req = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "status": "ABSENT",
            "punch_in": pin_ts,
            "reason": "Invalid correction",
            "regularized_by": "hr.admin",
        },
    )
    assert bad_req.status_code == 422

    # Changing to ABSENT without punch times succeeds and clears punch_in
    good_req = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "status": "ABSENT",
            "reason": "marked absent by mistake",
            "regularized_by": "hr.admin",
        },
    )
    assert good_req.status_code == 200
    data = good_req.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["late_minutes"] == 0
    assert data["work_hours"] is None


def test_regularize_no_change_422(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })
    pin_ts = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pin_ts})

    # Sending status PRESENT when already PRESENT -> 422
    resp = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "status": "PRESENT",
            "reason": "No actual change",
            "regularized_by": "hr.admin",
        },
    )
    assert resp.status_code == 422


def test_invalid_calendar_dates(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })

    # Invalid calendar date strings in GET /attendance
    assert client.get("/attendance?date_from=2026-02-31").status_code == 422
    assert client.get("/attendance?date_to=invalid-date").status_code == 422

    # Invalid date path parameter in PATCH /attendance/{emp_code}/{date}
    assert client.patch(
        "/attendance/EMP0001/2026-13-45",
        json={"reason": "test fix", "regularized_by": "admin", "status": "PRESENT"}
    ).status_code == 422


def test_epoch_millis_range_boundaries(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01",
    })

    # Min allowed: 100000000000 (valid)
    resp_min = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 100000000000})
    assert resp_min.status_code in (201, 409)

    # Below min: 99999999999 -> 422
    resp_below = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 99999999999})
    assert resp_below.status_code == 422

    # Above max: 4102444800001 -> 422
    resp_above = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 4102444800001})
    assert resp_above.status_code == 422


def test_legacy_record_serialization_defaults():
    from app.main import serialize_attendance_record
    legacy_doc = {
        "emp_code": "EMP0002",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc),
        # Notice missing half_day, history, late_minutes, overtime_minutes
    }
    serialized = serialize_attendance_record(legacy_doc)
    assert serialized["half_day"] is False
    assert serialized["history"] == []
    assert serialized["late_minutes"] == 0
    assert serialized["overtime_minutes"] == 0
    assert serialized["work_hours"] is None


def test_regularize_legacy_missing_history_and_half_day(client, mock_db):
    mock_db.employees.insert_one({
        "emp_code": "LEG001",
        "name": "Legacy Worker",
        "email": "legacy@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    mock_db.attendance_logs.insert_one({
        "emp_code": "LEG001",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc),
        "work_hours": 8.98,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })

    new_punch_in_ms = int(datetime(2026, 7, 14, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    resp = client.patch(
        "/attendance/LEG001/2026-07-14",
        json={
            "regularized_by": "hr.admin",
            "reason": "biometric sync",
            "punch_in": new_punch_in_ms,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["emp_code"] == "LEG001"
    assert len(data["history"]) == 1
    h0 = data["history"][0]
    assert h0["by"] == "hr.admin"
    assert h0["reason"] == "biometric sync"
    assert "half_day" not in h0["changes"]
    assert "punch_in" in h0["changes"]

