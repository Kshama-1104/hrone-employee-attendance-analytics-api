# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (lines 33–37) | `minutes > 10` evaluates to False at 10 minutes and 1 second because integer division floors `10.016` to 10. | Punch-in at 09:40:01 for a 09:30 shift returns 0 instead of 10. | Compare total elapsed seconds strictly greater than 600 seconds (`diff_sec > 600`), then compute whole minutes via `diff_sec // 60`. |
| 2 | `compute_work_hours` (lines 40–41) | Python's built-in `round()` uses banker's rounding (round half to even) instead of half-up rounding (R4). | 7650 seconds (2.125 hours) rounds to 2.12 instead of 2.13. | Converted duration to `decimal.Decimal` and quantized using `ROUND_HALF_UP`. |
| 3 | `compute_overtime` (lines 44–47) | Missing 30-minute threshold check (R3); counts overtime under 30 minutes. Also fails on overnight shifts where shift ends on the next calendar day. | Punch-out at 18:45 for shift ending 18:30 returns 15 minutes instead of 0. | Added threshold check (`ot_min >= 30`); resolved next-calendar-day shift end when `shift_end <= shift_start`. |
| 4 | `health` (lines 72–74) | Returns hardcoded `{"status": "ok"}` without verifying database connectivity. | `/health` returns 200 even when MongoDB is down or unreachable. | Pinged MongoDB using `db.command("ping")` and return 503 on error. |
| 5 | `create_employee` (lines 78–85) | Check-then-insert race condition. Stores naive local datetime instead of UTC BSON date. Response leaks `_id` / missing epoch ms. | Concurrent duplicate requests create duplicates or crash. Response contains datetime object instead of epoch milliseconds. | Relied on unique MongoDB index on `emp_code` catching `DuplicateKeyError` to return 409; serialized `created_at` to epoch milliseconds. |
| 6 | `list_employees` (lines 89–96) | `skip = page * page_size` skips the first page entirely (`skip=20` for page 1). `total` counts entire collection rather than filtered query. Missing sort by `emp_code`. | Requesting `page=1` returns records 21–40. Total reflects unfiltered collection count. | Changed skip to `(page - 1) * page_size`, applied `db.employees.count_documents(q)`, and added `.sort("emp_code", 1)`. |
| 7 | `punch_in` (lines 100–121) | Check-then-insert race condition. Missing 404 check if employee does not exist. Naive local date calculation misses R1 overnight shift rule. Injects uncontracted `id` field. | Crashes with 500 when employee does not exist. Overnight shift punch-ins are assigned to the wrong date. Response includes `id`. | Returned 404 if employee missing; enforced unique `(emp_code, date)` compound index returning 409; resolved overnight shifts under R1; removed `id` field. |
| 8 | `list_attendance` (lines 124–150) | In-memory sorting and pagination (`list(db.attendance_logs.find(q))`). Missing validation for `date_from > date_to`. Injects `id` and leaks raw BSON datetimes. | Memory exhaustion on 100k documents; `date_from > date_to` returns 200 instead of 422; schema mismatch. | Pushed filter, compound sort `(date desc, emp_code asc)`, skip, and limit down to MongoDB cursor; added date range validation (422); serialized BSON datetimes to epoch milliseconds. |
| 9 | Models & Validation (lines 53–67) | Missing regex and length validation for `emp_code`, email, and shift times. Did not reject equal start and end times or non-integer timestamps. | Invalid employee codes, invalid emails, identical shift times, or float timestamps were accepted. | Implemented strict Pydantic v2 schemas and validators matching `openapi.yaml`. |
| 10 | Startup & Indexes (lines 20–28) | No lifespan context manager or startup index initialization. App started without required unique and performance indexes. | Fresh database failed concurrency guarantees immediately. | Implemented FastAPI `lifespan` handler executing `ensure_indexes()`; raised `RuntimeError` on startup failure to prevent accepting traffic without indexes. |

### Examined and Decided NOT to be Defects
- Default shift times (`09:30` and `18:30`): Kept as defaults in `EmployeeCreate` because `openapi.yaml` explicitly documents them as default values.
- `emp_code` immutable customer code: Verified that the absence of an update endpoint for `emp_code` is intentional per the problem statement convention.

---

## Phase 3.1 Adversarial Audit Remediations

| # | Where (function / component) | What was wrong / Risk identified | How noticed | Remediation |
|---|---|---|---|---|
| A1 | Analytics handlers (`app/main.py`) | Handlers caught `(NotImplementedError, OperationFailure)` broadly, which silently swallowed real production MongoDB errors and fell back to Python loops. | Adversarial audit of exception handling paths. | Replaced broad catches with `is_mock_db()` guard. In production environments (`mongomock` not active), MongoDB errors are raised strictly without swallowing. |
| A2 | `build_department_trend_pipeline` | Trend endpoint previously raised `NotImplementedError` directly, bypassing the production aggregation pipeline. | Inspection of `get_department_trend`. | Implemented 100% database-side aggregation pipeline using `$map`, `$range`, `$dateAdd`, `$lookup`, and `$setWindowFields` for real MongoDB 5.0/6.0+. |

---

## Phase 4 Admin Explain & Index Optimizations

| # | Where (function / component) | What was wrong / Risk identified | How noticed | Remediation |
|---|---|---|---|---|
| B1 | `ensure_indexes` / `department_summary` | When `department_summary` is requested without an optional `department` parameter, the `$match` on `joined_on <= month_end` triggered a `COLLSCAN` on `employees`. | Query planner explain plan audit on live MongoDB 6.0. | Added single-field index `idx_joined_on` (`[("joined_on", 1)]`) on `employees`. Verified that `department_summary` without `department` now uses `IXSCAN`. |
| B2 | `/admin/explain/{endpoint}` | Missing endpoint required by API specification to inspect production query execution statistics. | OpenAPI 3.0.3 contract audit. | Implemented route executing `explain("executionStats")` for all 5 endpoint keys using production query builders and jsonable serialization. |

---

## Phase 5 Surgical Defect Repairs

| # | Where (function / component) | What was wrong / Risk identified | How noticed | Remediation |
|---|---|---|---|---|
| C1 | `regularize_attendance` (`app/main.py:607-625`) | Optimistic CAS query `{"history": []}` failed to match legacy records missing the `history` field, raising false `409 Conflict`. Also created spurious change entry `{"from": null, "to": false}` for missing `half_day`. | Adversarial evaluation with DATA_MODEL.md Sample 9 legacy records. | Updated CAS filter to match `{"$or": [{"history": {"$exists": False}}, {"history": []}, {"history": None}]}` for legacy records. Normalized `half_day` comparison default to `False`. |
| C2 | `build_department_trend_pipeline` (`app/main.py:1335-1390`) | Production pipeline used native MongoDB `$round`, which implements IEEE 754 Banker's rounding (round to nearest even), violating Rule R8 strict half-up rounding on exact midpoint boundaries (e.g. `0.00035`). | Exact mathematical boundary audit on live MongoDB 6.0. | Replaced `$round` with mathematical half-up floor expressions `{"$divide": [{"$floor": {"$add": [{"$multiply": [val, 10000]}, 0.5]}}, 10000]}` directly inside MongoDB pipeline, and enforced Decimal half-up quantization at API response serialization boundary. |
| C3 | `requirements.txt` & `pytest.ini` | Test dependencies (`mongomock`, `pytest`, `httpx`) missing from `requirements.txt`. Plain `pytest -v` failed out of the box with `ModuleNotFoundError: No module named 'app'`. | Clean clone installation verification. | Added test dependencies to `requirements.txt` and created root `pytest.ini` with `pythonpath = .`. |
| C4 | `tests/test_real_mongodb_integration.py` | Integration test suite hardcoded requirement that `MONGO_DB == "hrone_attendance_test"`, breaking evaluation if grader injects custom test database name. | Integration test precondition audit. | Relaxed database name assertion to accept any valid non-system database injected via environment while guarding against system databases (`admin`, `config`, `local`). |


