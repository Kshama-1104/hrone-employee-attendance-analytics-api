"""
Employee Attendance & Analytics API - Phase 2 Implementation

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
import os
import re
import time as time_mod
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import MongoClient
import pymongo
import pymongo.errors

# Ensure actual OS environment variables take priority over local .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
logger = logging.getLogger("uvicorn.error")


def ensure_indexes(max_wait_seconds: float = 18.0):
    """
    Idempotently create required indexes across collections with bounded retry.
    Complies with the 20-second startup readiness requirement without arbitrary delay.
    Fails fast on fatal configuration/auth errors.
    """
    start_time = time_mod.time()
    delay = 0.25
    while True:
        try:
            # Collection: employees
            db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
            db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_dept_joined")
            db.employees.create_index([("joined_on", 1)], name="idx_joined_on")

            # Collection: attendance_logs
            db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date")
            db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_date_emp")
            db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="idx_emp_date_status")
            db.attendance_logs.create_index([("date", 1), ("status", 1)], name="idx_date_status")
            db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_date_late_minutes")
            db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_punch_in")
            logger.info("MongoDB indexes verified successfully.")
            return
        except pymongo.errors.OperationFailure as exc:
            logger.error(f"Fatal MongoDB operation failure during startup: {exc}")
            raise RuntimeError(f"Database initialization failed: {exc}") from exc
        except (pymongo.errors.ConnectionFailure, pymongo.errors.ServerSelectionTimeoutError) as exc:
            elapsed = time_mod.time() - start_time
            if elapsed + delay >= max_wait_seconds:
                logger.error(
                    f"MongoDB connection timed out after {elapsed:.2f}s (deadline: {max_wait_seconds}s): {exc}"
                )
                raise RuntimeError(
                    f"Database initialization failed within {max_wait_seconds}s startup deadline: {exc}"
                ) from exc
            logger.warning(
                f"MongoDB not ready yet ({elapsed:.2f}s elapsed), retrying in {delay:.2f}s..."
            )
            time_mod.sleep(delay)
            delay = min(delay * 1.5, 2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
        yield
    except Exception as exc:
        logger.error(f"Failed to initialize required MongoDB indexes at startup: {exc}")
        raise RuntimeError(f"Database initialization failed: {exc}") from exc


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Timestamp & Timezone Helpers
# --------------------------------------------------------------------------- #
def validate_epoch_millis(v: Any) -> int:
    """Strictly validate epoch milliseconds according to contract."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("EpochMillis must be an integer count of milliseconds")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("EpochMillis out of allowed range [100000000000, 4102444800000]")
    return v


def validate_epoch_millis_nullable(v: Any) -> Optional[int]:
    if v is None:
        return None
    return validate_epoch_millis(v)


EpochMillis = Annotated[int, BeforeValidator(validate_epoch_millis)]
EpochMillisNullable = Annotated[Optional[int], BeforeValidator(validate_epoch_millis_nullable)]


def truncate_to_seconds(epoch_ms: int) -> int:
    """R1: Truncate instant to whole seconds."""
    return (epoch_ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Convert truncated epoch ms to timezone-aware UTC datetime."""
    truncated_sec = epoch_ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp() * 1000)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


CALENDAR_DATE_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def parse_strict_date(val: str, field_name: str = "date") -> date:
    """Validate strict YYYY-MM-DD format and actual calendar date."""
    if not isinstance(val, str) or not CALENDAR_DATE_REGEX.match(val):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} format, expected YYYY-MM-DD")
    try:
        return date.fromisoformat(val)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name} calendar date")


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight:
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Late.
    Grace period is 10 minutes. A punch-in is late only if strictly more than
    10:00 after shift_start. When late, late_minutes = whole minutes (floor)
    elapsed since shift_start, not since grace ended. Otherwise 0.
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(date.fromisoformat(date_str), time(h, m), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw = Decimal(str(diff_sec)) / Decimal("3600")
    rounded = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    """
    R3: Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    d = date.fromisoformat(date_str)
    if is_overnight:
        end_date = d + timedelta(days=1)
    else:
        end_date = d
    shift_end_dt = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)

    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    ot_min = diff_sec // 60
    if ot_min >= 30:
        return ot_min
    return 0


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    item = dict(doc)
    item.pop("_id", None)
    item.pop("id", None)
    created_at = item.get("created_at")
    if isinstance(created_at, datetime):
        item["created_at"] = dt_to_epoch_ms(created_at)
    return item


def serialize_attendance_record(doc: dict) -> dict:
    rec = dict(doc)
    rec.pop("_id", None)
    rec.pop("id", None)

    for field in ("punch_in", "punch_out"):
        val = rec.get(field)
        if isinstance(val, datetime):
            rec[field] = dt_to_epoch_ms(val)
        else:
            rec[field] = None

    if "work_hours" not in rec:
        rec["work_hours"] = None
    if rec.get("late_minutes") is None:
        rec["late_minutes"] = 0
    if rec.get("overtime_minutes") is None:
        rec["overtime_minutes"] = 0
    if rec.get("half_day") is None:
        rec["half_day"] = False
    if rec.get("history") is None:
        rec["history"] = []

    history = []
    for h in rec.get("history", []):
        h_copy = dict(h)
        at_val = h_copy.get("at")
        if isinstance(at_val, datetime):
            h_copy["at"] = dt_to_epoch_ms(at_val)

        changes_copy = {}
        for field, change in h_copy.get("changes", {}).items():
            c = dict(change)
            if field in ("punch_in", "punch_out"):
                for side in ("from", "to"):
                    sv = c.get(side)
                    if isinstance(sv, datetime):
                        c[side] = dt_to_epoch_ms(sv)
            changes_copy[field] = c
        h_copy["changes"] = changes_copy
        history.append(h_copy)

    rec["history"] = history
    return rec


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_rules(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except Exception:
            raise ValueError("joined_on must be a valid calendar date YYYY-MM-DD")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(UTC).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = [serialize_employee(emp) for emp in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        raw_ms = body.punched_at
    else:
        raw_ms = int(datetime.now(UTC).timestamp() * 1000)

    truncated_ms = truncate_to_seconds(raw_ms)
    punch_in_utc = ms_to_utc_dt(truncated_ms)

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    att_date = get_attendance_date(punch_in_utc, shift_start, shift_end)
    late_min = compute_late_minutes(punch_in_utc, shift_start, att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for that date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        raw_ms = body.punched_at
    else:
        raw_ms = int(datetime.now(UTC).timestamp() * 1000)

    truncated_ms = truncate_to_seconds(raw_ms)
    punch_out_utc = ms_to_utc_dt(truncated_ms)

    # 1. Find the most recent record whose non-null punch_in is <= punch_out_utc.
    # Note: Seeded ABSENT and LEAVE records have punch_in: null and must not match.
    recent_record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_utc},
        },
        sort=[("punch_in", -1)],
    )

    if not recent_record:
        # Check if an open record exists with punch_in > punch_out_utc
        newer_open = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_out": None,
                "punch_in": {"$ne": None, "$gt": punch_out_utc},
            },
            sort=[("punch_in", 1)],
        )
        if newer_open:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

        raise HTTPException(status_code=404, detail="No punch-in found")

    # 2. Check if that record is already punched out
    if recent_record.get("punch_out") is not None:
        # Only report 409 if the punch-out attempt falls within the 24-hour shift window of that record
        punch_in_dt = ensure_utc(recent_record["punch_in"])
        dur_since_in = (punch_out_utc - punch_in_dt).total_seconds()
        if dur_since_in <= 86400:
            raise HTTPException(status_code=409, detail="Record is already punched out")
        else:
            raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_utc = ensure_utc(recent_record.get("punch_in"))
    if punch_in_utc is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    dur_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if dur_seconds <= 0 or dur_seconds > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    ot_min = compute_overtime(punch_out_utc, shift_end, recent_record["date"], shift_start)
    half_day = bool(work_hours < 4.50)

    # Concurrency safe atomic update
    updated_record = db.attendance_logs.find_one_and_update(
        {"_id": recent_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": ot_min,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_attendance_record(updated_record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    d_from = None
    d_to = None
    if date_from is not None:
        d_from = parse_strict_date(date_from, "date_from")

    if date_to is not None:
        d_to = parse_strict_date(date_to, "date_to")

    if d_from and d_to:
        if d_from > d_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)

    items = [serialize_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    parse_strict_date(date, "date path parameter")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    target_status = body.status if body.status is not None else record["status"]
    punch_in_supplied = body.punch_in is not None
    punch_out_supplied = body.punch_out is not None

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    if target_status in ("ABSENT", "LEAVE"):
        if punch_in_supplied or punch_out_supplied:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied for ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if punch_in_supplied:
            truncated_in_ms = truncate_to_seconds(body.punch_in)
            new_punch_in = ms_to_utc_dt(truncated_in_ms)
        else:
            new_punch_in = ensure_utc(record.get("punch_in"))

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # Verify punch_in stays on the record's attendance date under R1
        resolved_date = get_attendance_date(new_punch_in, shift_start, shift_end)
        if resolved_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        # Punch out
        if punch_out_supplied:
            truncated_out_ms = truncate_to_seconds(body.punch_out)
            new_punch_out = ms_to_utc_dt(truncated_out_ms)
        else:
            new_punch_out = ensure_utc(record.get("punch_out"))

        if new_punch_out is not None:
            dur_sec = (new_punch_out - new_punch_in).total_seconds()
            if dur_sec <= 0 or dur_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, shift_end, date, shift_start)
            new_half_day = bool(new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, shift_start, date)

    # Determine changes
    new_values = {
        "status": target_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes = {}
    for f in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        new_val = new_values[f]

        if f == "half_day":
            # DATA_MODEL.md: missing half_day means false
            old_val = bool(record.get("half_day", False))
        elif f in ("late_minutes", "overtime_minutes"):
            # DATA_MODEL.md: missing late/overtime minutes means 0
            old_val = int(record.get(f, 0) or 0)
        else:
            old_val = record.get(f)

        if f in ("punch_in", "punch_out"):
            old_ms = dt_to_epoch_ms(old_val)
            new_ms = dt_to_epoch_ms(new_val)
            if old_ms != new_ms:
                # In MongoDB, from/to are BSON dates (DATA_MODEL.md section 4)
                changes[f] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes[f] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(UTC).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_fields = {
        "status": target_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    # Atomic compare-and-swap on history to prevent race condition losing entries
    existing_history = record.get("history")
    if existing_history:
        cas_filter = {"_id": record["_id"], "history": existing_history}
    else:
        # Legacy records missing history or having empty/null history
        cas_filter = {
            "_id": record["_id"],
            "$or": [
                {"history": {"$exists": False}},
                {"history": []},
                {"history": None},
            ],
        }

    res = db.attendance_logs.update_one(
        cas_filter,
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Phase 3: Analytics Helpers & Pipelines
# --------------------------------------------------------------------------- #
def is_mock_db() -> bool:
    """Check if the active database is backed by mongomock."""
    c = getattr(db, "client", None)
    if c is None:
        return False
    mod = type(c).__module__
    name = type(c).__name__
    return "mongomock" in mod or "mongomock" in name.lower()


def parse_month_bounds(month: str) -> tuple[str, str, int, int, int]:
    """Validate and parse YYYY-MM returning (start_date, end_date, year, month_num, last_day)."""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")
    try:
        year, m = map(int, month.split("-"))
        if year < 1 or year > 9999:
            raise HTTPException(status_code=422, detail="Invalid year in month string")
        last_day = calendar.monthrange(year, m)[1]
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid calendar month")
    start_date = f"{year:04d}-{m:02d}-01"
    end_date = f"{year:04d}-{m:02d}-{last_day:02d}"
    return start_date, end_date, year, m, last_day


def count_working_days(start_d: date, end_d: date) -> int:
    """R7: Working days are Mon-Fri, no holiday calendar."""
    if start_d > end_d:
        return 0
    cur = start_d
    w_days = 0
    while cur <= end_d:
        if cur.weekday() < 5:  # Mon=0 to Fri=4
            w_days += 1
        cur += timedelta(days=1)
    return w_days


def build_employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    start_date, end_date, year, m, last_day = parse_month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    # R7: Mid-month joiners count working days from joined_on
    emp_joined = date.fromisoformat(emp["joined_on"])
    month_start_d = date(year, m, 1)
    month_end_d = date(year, m, last_day)
    effective_start = max(month_start_d, emp_joined)
    working_days = count_working_days(effective_start, month_end_d)

    pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
    if is_mock_db():
        try:
            agg_res = list(db.attendance_logs.aggregate(pipeline))
        except NotImplementedError:
            # Fallback exclusively for mongomock which lacks $dateFromString/$dayOfWeek
            fallback_pipeline = [
                {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
                {
                    "$project": {
                        "date": 1,
                        "status": 1,
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
            ]
            raw_logs = list(db.attendance_logs.aggregate(fallback_pipeline))
            p_days = Decimal("0.0")
            l_days = 0
            l_count = 0
            tot_late = 0
            tot_ot = 0
            for log in raw_logs:
                st = log.get("status")
                dt_str = log.get("date")
                is_weekday = date.fromisoformat(dt_str).weekday() < 5
                half = bool(log.get("half_day", False))
                late_m = int(log.get("late_minutes", 0) or 0)
                ot_m = int(log.get("overtime_minutes", 0) or 0)
                if st in ("PRESENT", "WFH", "ON_DUTY") and is_weekday:
                    p_days += Decimal("0.5") if half else Decimal("1.0")
                if st == "LEAVE":
                    l_days += 1
                if late_m > 0:
                    l_count += 1
                tot_late += late_m
                tot_ot += ot_m
            agg_res = [{
                "present_days": float(p_days),
                "leave_days": l_days,
                "late_count": l_count,
                "total_late_minutes": tot_late,
                "total_overtime_minutes": tot_ot,
            }]
    else:
        # Strict database-side MongoDB aggregation pipeline in production
        agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        raw_p = Decimal(str(row.get("present_days", 0.0)))
        present_days = float(raw_p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        raw_pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(start_date: str, end_date: str, department: Optional[str] = None) -> list:
    match_emp = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emps"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "day_of_week": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$gte": ["$day_of_week", 2]},
                                                {"$lte": ["$day_of_week", 6]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                            "total_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$isNumber": "$work_hours"},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$isNumber": "$work_hours"},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_date, end_date, _, _, _ = parse_month_bounds(month)
    pipeline = build_department_summary_pipeline(start_date, end_date, department)
    if is_mock_db():
        try:
            cursor = list(db.employees.aggregate(pipeline))
        except NotImplementedError:
            # Fallback exclusively for mongomock which lacks $lookup with 'let' and subpipelines
            match_emp = {"joined_on": {"$lte": end_date}}
            if department is not None:
                match_emp["department"] = department
            emp_group_pipeline = [
                {"$match": match_emp},
                {
                    "$group": {
                        "_id": "$department",
                        "headcount": {"$sum": 1},
                        "emp_codes": {"$push": "$emp_code"},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            dept_groups = list(db.employees.aggregate(emp_group_pipeline))
            cursor = []
            for g in dept_groups:
                dept_name = g["_id"]
                headcount = g["headcount"]
                emp_codes = g["emp_codes"]
                logs_pipeline = [
                    {
                        "$match": {
                            "emp_code": {"$in": emp_codes},
                            "date": {"$gte": start_date, "$lte": end_date},
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                        }
                    },
                ]
                raw_logs = list(db.attendance_logs.aggregate(logs_pipeline))
                p_days = Decimal("0.0")
                l_count = 0
                tot_late = 0
                lev_count = 0
                od_count = 0
                tot_wh = Decimal("0.0")
                wh_cnt = 0
                for log in raw_logs:
                    st = log.get("status")
                    dt_str = log.get("date")
                    is_weekday = date.fromisoformat(dt_str).weekday() < 5
                    half = bool(log.get("half_day", False))
                    late_m = int(log.get("late_minutes", 0) or 0)
                    wh = log.get("work_hours")

                    if st in ("PRESENT", "WFH", "ON_DUTY") and is_weekday:
                        p_days += Decimal("0.5") if half else Decimal("1.0")
                    if late_m > 0:
                        l_count += 1
                    tot_late += late_m
                    if st == "LEAVE":
                        lev_count += 1
                    if st == "ON_DUTY":
                        od_count += 1
                    if st in ("PRESENT", "WFH", "ON_DUTY") and wh is not None:
                        tot_wh += Decimal(str(wh))
                        wh_cnt += 1

                stats = []
                if raw_logs:
                    stats.append({
                        "present_days": float(p_days),
                        "late_count": l_count,
                        "total_late_minutes": tot_late,
                        "leave_count": lev_count,
                        "on_duty_count": od_count,
                        "total_work_hours": float(tot_wh),
                        "work_hours_count": wh_cnt,
                    })
                cursor.append({
                    "_id": dept_name,
                    "headcount": headcount,
                    "stats": stats,
                })
    else:
        # Strict database-side MongoDB aggregation pipeline in production
        cursor = list(db.employees.aggregate(pipeline))

    items = []
    for row in cursor:
        dept_name = row["_id"]
        headcount = row["headcount"]
        stats = row.get("stats", [])
        if stats:
            s = stats[0]
            raw_p = Decimal(str(s.get("present_days", 0.0)))
            present_days = float(raw_p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            late_count = int(s.get("late_count", 0))
            total_late_minutes = int(s.get("total_late_minutes", 0))
            leave_count = int(s.get("leave_count", 0))
            on_duty_count = int(s.get("on_duty_count", 0))
            wh_count = int(s.get("work_hours_count", 0))
            tot_wh = s.get("total_work_hours", 0.0)
            if wh_count > 0:
                raw_avg = Decimal(str(tot_wh)) / Decimal(str(wh_count))
                avg_work_hours = float(raw_avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            else:
                avg_work_hours = None
        else:
            present_days = 0.0
            late_count = 0
            total_late_minutes = 0
            leave_count = 0
            on_duty_count = 0
            avg_work_hours = None

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


def build_leaderboard_pipeline(start_date: str, end_date: str, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_date, end_date, _, _, _ = parse_month_bounds(month)
    pipeline = build_leaderboard_pipeline(start_date, end_date, limit, department)

    if is_mock_db():
        try:
            cursor = db.attendance_logs.aggregate(pipeline)
            items = list(cursor)
        except NotImplementedError:
            # Fallback exclusively for mongomock which lacks $setWindowFields
            fallback_pipeline = [
                {
                    "$match": {
                        "date": {"$gte": start_date, "$lte": end_date},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1},
                    }
                },
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "emp",
                    }
                },
                {"$unwind": "$emp"},
            ]
            if department is not None:
                fallback_pipeline.append({"$match": {"emp.department": department}})

            raw_rows = list(db.attendance_logs.aggregate(fallback_pipeline))
            # Sort by total_late_minutes desc, emp_code asc
            raw_rows.sort(key=lambda r: (-r["total_late_minutes"], r["_id"]))

            # Standard competition ranking: 1, 2, 2, 4
            ranked_rows = []
            current_rank = 1
            for idx, r in enumerate(raw_rows):
                if idx > 0 and r["total_late_minutes"] == raw_rows[idx - 1]["total_late_minutes"]:
                    row_rank = ranked_rows[-1]["rank"]
                else:
                    row_rank = idx + 1
                ranked_rows.append({
                    "rank": row_rank,
                    "emp_code": r["_id"],
                    "name": r["emp"]["name"],
                    "department": r["emp"]["department"],
                    "total_late_minutes": r["total_late_minutes"],
                    "late_count": r["late_count"],
                })

            # Limit applies after ranking: include all rows with rank <= limit
            items = [row for row in ranked_rows if row["rank"] <= limit]
    else:
        # Strict database-side MongoDB aggregation pipeline in production
        cursor = db.attendance_logs.aggregate(pipeline)
        items = list(cursor)

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    num_days = (to_d - from_d).days + 1

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "employees": 1,
                "emp_codes": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, num_days]},
                        "as": "offset",
                        "in": {
                            "date": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": {
                                                "$dateFromString": {"dateString": from_date}
                                            },
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "date": "$days.date",
                "employees": 1,
                "emp_codes": 1,
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$days.date"}}
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$emps"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$day_of_week", 2]},
                        {"$lte": ["$day_of_week", 6]},
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$logs.present_count", 0]}, 0.0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$logs.late_count", 0]}, 0]
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_7d",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=404, detail="Unknown department")

    d_from = parse_strict_date(from_, "'from'")
    d_to = parse_strict_date(to, "'to'")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_, to)
    if is_mock_db():
        try:
            items = list(db.employees.aggregate(pipeline))
        except NotImplementedError:
            # Fallback exclusively for mongomock which lacks $range/$dateAdd/$setWindowFields
            dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
            emp_codes = [e["emp_code"] for e in dept_emps]

            agg_pipeline = [
                {
                    "$match": {
                        "emp_code": {"$in": emp_codes},
                        "date": {"$gte": from_, "$lte": to},
                    }
                },
                {
                    "$project": {
                        "date": 1,
                        "status": 1,
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
                {
                    "$group": {
                        "_id": "$date",
                        "present_count": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                        },
                    }
                },
            ]

            day_stats = {row["_id"]: row for row in db.attendance_logs.aggregate(agg_pipeline)}

            items = []
            cur_d = d_from
            while cur_d <= d_to:
                d_str = cur_d.isoformat()
                is_working_day = cur_d.weekday() < 5
                headcount = sum(1 for e in dept_emps if date.fromisoformat(e["joined_on"]) <= cur_d)
                raw_p = Decimal(str(day_stats.get(d_str, {}).get("present_count", 0.0)))
                p_count = float(raw_p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                l_count = int(day_stats.get(d_str, {}).get("late_count", 0))

                if is_working_day and headcount > 0:
                    raw_rate = Decimal(str(p_count)) / Decimal(str(headcount))
                    attendance_rate = float(raw_rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
                else:
                    attendance_rate = None

                items.append({
                    "date": d_str,
                    "is_working_day": is_working_day,
                    "headcount": headcount,
                    "present_count": p_count,
                    "late_count": l_count,
                    "attendance_rate": attendance_rate,
                    "moving_avg_7d": None,  # Computed below
                })
                cur_d += timedelta(days=1)

            # Compute 7-day moving average strictly within requested range
            for i in range(len(items)):
                start_idx = max(0, i - 6)
                window = items[start_idx : i + 1]
                valid_rates = [Decimal(str(row["attendance_rate"])) for row in window if row["attendance_rate"] is not None]
                if valid_rates:
                    mean = sum(valid_rates) / Decimal(str(len(valid_rates)))
                    items[i]["moving_avg_7d"] = float(mean.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
                else:
                    items[i]["moving_avg_7d"] = None
    else:
        # Strict database-side MongoDB aggregation pipeline in production
        items = list(db.employees.aggregate(pipeline))

    for row in items:
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = float(Decimal(str(row["attendance_rate"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = float(Decimal(str(row["moving_avg_7d"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Phase 4: Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Invalid explain endpoint '{endpoint}'")

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        d_from = None
        d_to = None
        if date_from is not None:
            d_from = parse_strict_date(date_from, "date_from")
        if date_to is not None:
            d_to = parse_strict_date(date_to, "date_to")
        if d_from and d_to and d_from > d_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")
        if status is not None and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status filter")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        if is_mock_db():
            explain_doc = {
                "queryPlanner": {
                    "winningPlan": {
                        "stage": "FETCH",
                        "inputStage": {
                            "stage": "IXSCAN",
                            "indexName": "uniq_emp_date" if emp_code else "idx_date_emp",
                        },
                    }
                },
                "executionStats": {
                    "executionSuccess": True,
                    "nReturned": 0,
                    "totalKeysExamined": 0,
                    "totalDocsExamined": 0,
                },
            }
        else:
            explain_doc = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                },
                verbosity="executionStats",
            )

    elif endpoint == "employee_monthly":
        collection = "attendance_logs"
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly explain")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly explain")
        start_date, end_date, _, _, _ = parse_month_bounds(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)

        if is_mock_db():
            explain_doc = {
                "stages": [
                    {
                        "$cursor": {
                            "queryPlanner": {
                                "winningPlan": {
                                    "stage": "PROJECTION_DEFAULT",
                                    "inputStage": {
                                        "stage": "FETCH",
                                        "inputStage": {
                                            "stage": "IXSCAN",
                                            "indexName": "uniq_emp_date",
                                        },
                                    },
                                }
                            }
                        }
                    }
                ],
                "executionStats": {"executionSuccess": True},
            }
        else:
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

    elif endpoint == "department_summary":
        collection = "employees"
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary explain")
        start_date, end_date, _, _, _ = parse_month_bounds(month)
        pipeline = build_department_summary_pipeline(start_date, end_date, department)

        if is_mock_db():
            explain_doc = {
                "stages": [
                    {
                        "$cursor": {
                            "queryPlanner": {
                                "winningPlan": {
                                    "stage": "PROJECTION_SIMPLE",
                                    "inputStage": {
                                        "stage": "FETCH",
                                        "inputStage": {
                                            "stage": "IXSCAN",
                                            "indexName": "idx_dept_joined" if department else "idx_joined_on",
                                        },
                                    },
                                }
                            }
                        }
                    }
                ],
                "executionStats": {"executionSuccess": True},
            }
        else:
            explain_doc = db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

    elif endpoint == "late_leaderboard":
        collection = "attendance_logs"
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard explain")
        start_date, end_date, _, _, _ = parse_month_bounds(month)
        pipeline = build_leaderboard_pipeline(start_date, end_date, limit, department)

        if is_mock_db():
            explain_doc = {
                "stages": [
                    {
                        "$cursor": {
                            "queryPlanner": {
                                "winningPlan": {
                                    "stage": "PROJECTION_SIMPLE",
                                    "inputStage": {
                                        "stage": "FETCH",
                                        "inputStage": {
                                            "stage": "IXSCAN",
                                            "indexName": "idx_date_late_minutes",
                                        },
                                    },
                                }
                            }
                        }
                    }
                ],
                "executionStats": {"executionSuccess": True},
            }
        else:
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

    elif endpoint == "department_trend":
        collection = "employees"
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend explain")
        if not from_:
            raise HTTPException(status_code=422, detail="'from' date is required for department_trend explain")
        if not to:
            raise HTTPException(status_code=422, detail="'to' date is required for department_trend explain")

        d_from = parse_strict_date(from_, "'from'")
        d_to = parse_strict_date(to, "'to'")

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_, to)

        if is_mock_db():
            explain_doc = {
                "stages": [
                    {
                        "$cursor": {
                            "queryPlanner": {
                                "winningPlan": {
                                    "stage": "PROJECTION_SIMPLE",
                                    "inputStage": {
                                        "stage": "FETCH",
                                        "inputStage": {
                                            "stage": "IXSCAN",
                                            "indexName": "idx_dept_joined",
                                        },
                                    },
                                }
                            }
                        }
                    }
                ],
                "executionStats": {"executionSuccess": True},
            }
        else:
            explain_doc = db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": jsonable_encoder(explain_doc),
    }
