"""
Employee Attendance & Analytics API - Phase 2 Implementation

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
import os
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import MongoClient
import pymongo
import pymongo.errors

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
logger = logging.getLogger("uvicorn.error")


def ensure_indexes():
    """Idempotently create required indexes across collections."""
    # Collection: employees
    db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_dept_joined")

    # Collection: attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_date_emp")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="idx_emp_date_status")
    db.attendance_logs.create_index([("date", 1), ("status", 1)], name="idx_date_status")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_punch_in")
    logger.info("MongoDB indexes verified successfully.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:
        logger.error(f"Failed to initialize required MongoDB indexes at startup: {exc}")
        raise RuntimeError(f"Database initialization failed: {exc}") from exc
    yield


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

    # Find the most recent open record whose punch_in <= punch_out_utc
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not open_record:
        # Check if an open record exists with punch_in > punch_out_utc
        newer_open = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$gt": punch_out_utc}},
            sort=[("punch_in", 1)],
        )
        if newer_open:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

        # Check if the latest record is already punched out
        latest_record = db.attendance_logs.find_one({"emp_code": body.emp_code}, sort=[("punch_in", -1)])
        if latest_record and latest_record.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="Record is already punched out")

        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_utc = ensure_utc(open_record["punch_in"])

    dur_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if dur_seconds <= 0 or dur_seconds > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    ot_min = compute_overtime(punch_out_utc, shift_end, open_record["date"], shift_start)
    half_day = bool(work_hours < 4.50)

    # Concurrency safe atomic update
    updated_record = db.attendance_logs.find_one_and_update(
        {"_id": open_record["_id"], "punch_out": None},
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
    if date_from is not None:
        try:
            date.fromisoformat(date_from)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_from calendar date")

    if date_to is not None:
        try:
            date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_to calendar date")

    if date_from and date_to:
        if date_from > date_to:
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
    try:
        from datetime import date as date_cls
        date_cls.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date path parameter")

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
        old_val = record.get(f)
        new_val = new_values[f]

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
    existing_history = record.get("history", [])
    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": existing_history},
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance_record(updated)
