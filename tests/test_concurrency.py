import concurrent.futures
from datetime import datetime
import mongomock
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app, UTC


@pytest.fixture
def thread_safe_db():
    client = mongomock.MongoClient()
    db = client["concurrency_test_db"]
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    with patch("app.main.db", db), patch("app.main.client", client):
        yield db


def test_concurrent_employee_creation_race(thread_safe_db):
    """Simultaneous requests with identical emp_code yield exactly one 201 and all others 409."""
    client = TestClient(app)
    payload = {
        "emp_code": "EMP0100",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    }

    def attempt_create(_):
        return client.post("/employees", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_create, range(10)))

    assert results.count(201) == 1
    assert results.count(409) == 9


def test_concurrent_punch_in_race(thread_safe_db):
    """Two or more identical punch-in requests at the same instant yield exactly one 201 and others 409."""
    client = TestClient(app)
    # Create employee first
    client.post("/employees", json={
        "emp_code": "EMP0101",
        "name": "Punch Tester",
        "email": "punch@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })

    ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    payload = {"emp_code": "EMP0101", "punched_at": ts}

    def attempt_punch_in(_):
        return client.post("/attendance/punch-in", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_punch_in, range(10)))

    assert results.count(201) == 1
    assert results.count(409) == 9


def test_concurrent_punch_out_race(thread_safe_db):
    """Simultaneous punch-out requests for the same open record yield exactly one 200 and others 409."""
    client = TestClient(app)
    client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Out Tester",
        "email": "out@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })
    pin_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0102", "punched_at": pin_ts})

    pout_ts = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
    payload = {"emp_code": "EMP0102", "punched_at": pout_ts}

    def attempt_punch_out(_):
        return client.post("/attendance/punch-out", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_punch_out, range(10)))

    assert results.count(200) == 1
    assert results.count(409) == 9


def test_concurrent_regularization_conflict_protection(thread_safe_db):
    """Concurrent regularizations detect race and do not overwrite or corrupt history silently."""
    client = TestClient(app)
    client.post("/employees", json={
        "emp_code": "EMP0103",
        "name": "Reg Tester",
        "email": "reg@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })
    pin_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0103", "punched_at": pin_ts})

    def attempt_reg(i):
        # Each thread attempts to change punch_in slightly
        new_ts = pin_ts - (i * 60 * 1000)
        resp = client.patch(
            "/attendance/EMP0103/2026-07-06",
            json={
                "punch_in": new_ts,
                "reason": f"Correction by admin {i}",
                "regularized_by": f"admin_{i}",
            },
        )
        return resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(attempt_reg, range(1, 6)))

    # Every attempt was either 200 (serialized) or 409 (conflict detected), no 500s
    assert all(code in (200, 409) for code in results)
    assert 200 in results
