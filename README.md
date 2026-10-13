# HROne Employee Attendance & Analytics API

FastAPI and MongoDB backend implementation of the HROne Employee Attendance & Analytics API assignment (API Contract v2.0).

---

## 1. Prerequisites & Tech Stack

- **Python:** 3.11+
- **Database:** MongoDB 6.0+ (MongoDB 6.0 or 7.0 required for `$setWindowFields`, dynamic `$range`, `$map`, and correlated subpipelines).
- **Key Dependencies:** `fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`.

---

## 2. Installation & Local Setup

### 2.1 Virtual Environment & Dependencies
```bash
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2 Environment Configuration
Create a `.env` file in the project root (ignored by Git):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
For integration testing against a dedicated test database:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hrone_attendance_test
```

### 2.3 Starting the Application
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation is available at `http://localhost:8000/docs`.

---

## 3. API Endpoints Overview

| Category | Method & Endpoint | Description |
|---|---|---|
| **Health** | `GET /health` | Database ping check; returns 200 `{"status": "ok"}` or 503. |
| **Employees** | `POST /employees` | Register employee; enforces unique `emp_code` (409 on conflict). |
| | `GET /employees` | Paginated listing sorted by `emp_code asc` with department filter. |
| **Attendance** | `POST /attendance/punch-in` | Punch-in with overnight shift date (R1) & late minutes (R2). |
| | `POST /attendance/punch-out` | Punch-out with work hours (R4), overtime (R3), and half-day (R5). |
| | `GET /attendance` | High-perf attendance log query with compound cursor sorting. |
| | `PATCH /attendance/{emp_code}/{date}` | Regularize attendance record with atomic audit history tracking. |
| **Analytics** | `GET /analytics/employees/{emp_code}/monthly` | Monthly employee summary with mid-month joiner & R7 working days. |
| | `GET /analytics/departments/summary` | Department cross-collection summary (R9) with zero-log headcount. |
| | `GET /analytics/leaderboard/late` | Competition rank (`1, 2, 2, 4`) via `$setWindowFields` with cutoff ties. |
| | `GET /analytics/departments/{department}/trend` | Daily date sequence with dynamic headcount and 7-day moving average. |
| **Admin** | `GET /admin/explain/{endpoint}` | Raw MongoDB `executionStats` explain plan for production queries. |

---

## 4. Testing

The repository maintains a strict separation between mock-based unit tests and real MongoDB integration tests:

### 4.1 Unit & Contract Tests (mongomock)
These tests execute completely in-memory using `mongomock` without requiring a running MongoDB server:
```bash
pytest tests/test_analytics.py tests/test_concurrency.py tests/test_foundations.py tests/test_phase2_apis.py tests/test_explain.py -v
```

### 4.2 Integration Tests (Real MongoDB 6.0+)
These tests execute directly against a live MongoDB 6.0+ instance (respecting `MONGO_URI` and `MONGO_DB` configured in the environment):
```bash
pytest tests/test_real_mongodb_integration.py -v
```
Verifies server version (&ge; 6.0), startup index creation, production aggregation pipelines, transaction/race-safe concurrency, legacy record regularization, strict half-up rounding at exact midpoints, and `/admin/explain/{endpoint}` plan outputs.

### 4.3 Full Test Suite
To run the full suite (unit + real integration tests):
```bash
pytest -v
```

---

## 5. Architecture & Deliverables

- `app/main.py`: Complete production implementation (FastAPI routes, Pydantic models, aggregation pipelines).
- `REVIEW.md`: Line-by-line defect audit and remediation log of starter code defects.
- `DECISIONS.md`: Plain-language architectural decisions answering the five core assignment questions.
- `requirements.txt`: Python package runtime requirements.
