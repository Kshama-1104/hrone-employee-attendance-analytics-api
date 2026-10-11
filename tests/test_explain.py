import mongomock
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


def test_explain_invalid_endpoint_422(client):
    res = client.get("/admin/explain/invalid_endpoint")
    assert res.status_code == 422
    assert "Invalid explain endpoint" in res.json()["detail"]


def test_explain_missing_required_parameters_422(client):
    # employee_monthly requires emp_code and month
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422
    assert client.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422

    # department_summary requires month
    assert client.get("/admin/explain/department_summary").status_code == 422

    # late_leaderboard requires month
    assert client.get("/admin/explain/late_leaderboard").status_code == 422

    # department_trend requires department, from, to
    assert client.get("/admin/explain/department_trend").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01").status_code == 422

    # attendance_list invalid dates
    assert client.get("/admin/explain/attendance_list?date_from=invalid").status_code == 422
    assert client.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-01").status_code == 422


def test_explain_all_five_endpoints_mock_schema(client):
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-10")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["endpoint"] == "attendance_list"
    assert d1["collection"] == "attendance_logs"
    assert "explain" in d1

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["endpoint"] == "employee_monthly"
    assert d2["collection"] == "attendance_logs"
    assert "explain" in d2

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["endpoint"] == "department_summary"
    assert d3["collection"] == "employees"
    assert "explain" in d3

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
    assert res4.status_code == 200
    d4 = res4.json()
    assert d4["endpoint"] == "late_leaderboard"
    assert d4["collection"] == "attendance_logs"
    assert "explain" in d4

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
    assert res5.status_code == 200
    d5 = res5.json()
    assert d5["endpoint"] == "department_trend"
    assert d5["collection"] == "employees"
    assert "explain" in d5
