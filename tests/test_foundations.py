from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from app.main import app, ensure_indexes


def test_health_check_success():
    with patch("app.main.ensure_indexes"), patch("app.main.db.command", return_value={"ok": 1}):
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}


def test_health_check_failure():
    with patch("app.main.ensure_indexes"), patch("app.main.db.command", side_effect=Exception("Connection refused")):
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 503
            assert response.json() == {"detail": "Database unavailable"}


def test_ensure_indexes_creates_expected_indexes():
    mock_db = MagicMock()
    with patch("app.main.db", mock_db):
        ensure_indexes()
        
        # Verify employees indexes
        emp_calls = mock_db.employees.create_index.call_args_list
        emp_index_keys = [call[0][0] for call in emp_calls]
        assert [("emp_code", 1)] in emp_index_keys
        assert [("department", 1), ("joined_on", 1)] in emp_index_keys
        
        # Verify emp_code unique constraint
        uniq_call = [call for call in emp_calls if call[0][0] == [("emp_code", 1)]][0]
        assert uniq_call[1].get("unique") is True

        # Verify attendance_logs indexes
        att_calls = mock_db.attendance_logs.create_index.call_args_list
        att_index_keys = [call[0][0] for call in att_calls]
        assert [("emp_code", 1), ("date", 1)] in att_index_keys
        assert [("date", -1), ("emp_code", 1)] in att_index_keys
        assert [("emp_code", 1), ("date", 1), ("status", 1)] in att_index_keys
        assert [("date", 1), ("status", 1)] in att_index_keys
        assert [("emp_code", 1), ("punch_in", -1)] in att_index_keys

        # Verify (emp_code, date) unique constraint
        uniq_att_call = [call for call in att_calls if call[0][0] == [("emp_code", 1), ("date", 1)]][0]
        assert uniq_att_call[1].get("unique") is True


def test_lifespan_startup_fails_when_index_creation_fails():
    with patch("app.main.ensure_indexes", side_effect=Exception("Disk full / index failure")):
        with pytest.raises(RuntimeError) as exc_info:
            with TestClient(app):
                pass
        assert "Database initialization failed" in str(exc_info.value)
