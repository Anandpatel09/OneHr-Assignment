import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EMP_RE = re.compile(r"^EMP\d{4,6}$")
SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    db.command("ping")
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)])


def parse_date(value: str) -> date:
    if not DATE_RE.match(value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)


def parse_month(value: str) -> tuple[str, date, date]:
    if not MONTH_RE.match(value):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return value, start, end


def validate_date_param(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        parse_date(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    return value


def shift_minutes(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def parse_shift(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() // 1 * 1000)


def trunc_epoch_ms_to_dt(value: Optional[int]) -> datetime:
    if value is None:
        value = now_ms()
    seconds = value // 1000
    return datetime.fromtimestamp(seconds, UTC)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def now_dt() -> datetime:
    return trunc_epoch_ms_to_dt(now_ms())


def q2(value: Any) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def q4(value: Any) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    att_date = local.date()
    if shift_minutes(shift_end) <= shift_minutes(shift_start):
        end_t = parse_shift(shift_end)
        if local.timetz().replace(tzinfo=None) < end_t:
            att_date -= timedelta(days=1)
    return att_date.isoformat()


def shift_boundary(att_date: str, shift: str, overnight_end: bool = False) -> datetime:
    d = parse_date(att_date)
    if overnight_end:
        d += timedelta(days=1)
    local = datetime.combine(d, parse_shift(shift), tzinfo=IST)
    return local.astimezone(UTC)


def shift_datetimes(att_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    overnight = shift_minutes(shift_end) <= shift_minutes(shift_start)
    return shift_boundary(att_date, shift_start), shift_boundary(att_date, shift_end, overnight)


def compute_late_minutes(punch_in: Optional[datetime], att_date: str, shift_start: str) -> int:
    if punch_in is None:
        return 0
    start = shift_boundary(att_date, shift_start)
    seconds = (punch_in - start).total_seconds()
    if seconds <= 10 * 60:
        return 0
    return math.floor(seconds / 60)


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    return q2(Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600))


def compute_overtime(punch_out: Optional[datetime], att_date: str, shift_start: str, shift_end: str) -> int:
    if punch_out is None:
        return 0
    _, end = shift_datetimes(att_date, shift_start, shift_end)
    minutes = math.floor((punch_out - end).total_seconds() / 60)
    return minutes if minutes >= 30 else 0


def compute_derived(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, att_date: str) -> dict:
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, att_date, emp["shift_start"]),
        "overtime_minutes": compute_overtime(punch_out, att_date, emp["shift_start"], emp["shift_end"]),
        "half_day": bool(work_hours is not None and Decimal(str(work_hours)) < Decimal("4.50")),
    }


def ensure_punch_out_window(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")


def scrub_doc(doc: dict) -> dict:
    result = {}
    for key, value in doc.items():
        if key == "_id":
            continue
        if isinstance(value, datetime):
            result[key] = dt_to_ms(value)
        elif isinstance(value, list):
            result[key] = [scrub_doc(v) if isinstance(v, dict) else v for v in value]
        elif isinstance(value, dict):
            result[key] = scrub_doc(value)
        else:
            result[key] = value
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    return result


def working_days(joined_on: str, start: date, end: date) -> int:
    cur = max(parse_date(joined_on), start)
    if cur > end:
        return 0
    count = 0
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def month_match(start: date, end: date) -> dict:
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


def epoch_field(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise ValueError(f"{field_name} must be epoch milliseconds")
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, value: str) -> str:
        if not EMP_RE.match(value):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise ValueError("invalid email")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, value: str) -> str:
        if not SHIFT_RE.match(value):
            raise ValueError("shift must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return epoch_field(value, "punched_at")

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return epoch_field(value, "punched_at")


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, value: Optional[int], info) -> Optional[int]:
        return epoch_field(value, info.field_name)


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return scrub_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = [scrub_doc(d) for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    punch = trunc_epoch_ms_to_dt(body.punched_at)
    att_date = attendance_date_for(punch, emp["shift_start"], emp["shift_end"])
    derived = compute_derived(body.status, punch, None, emp, att_date)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch,
        "punch_out": None,
        **derived,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return scrub_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = validate_date_param(date_from, "date_from")
    date_to = validate_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    return {"items": [scrub_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    punch = trunc_epoch_ms_to_dt(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch}, "status": {"$in": list(PRESENCE_STATUSES)}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    ensure_punch_out_window(rec["punch_in"], punch)
    derived = compute_derived(rec["status"], rec["punch_in"], punch, emp, rec["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": punch, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record is already punched out")
    return scrub_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    att_date = validate_date_param(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not rec:
        raise HTTPException(404, "attendance record not found")

    next_doc = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    supplied = body.model_fields_set
    if "status" in supplied and body.status is not None:
        next_doc["status"] = body.status
    if next_doc["status"] in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        next_doc["punch_in"] = None
        next_doc["punch_out"] = None
    else:
        if "punch_in" in supplied:
            if body.punch_in is None:
                raise HTTPException(422, "punch_in must be epoch milliseconds")
            next_doc["punch_in"] = trunc_epoch_ms_to_dt(body.punch_in)
        if "punch_out" in supplied:
            if body.punch_out is None:
                raise HTTPException(422, "punch_out must be epoch milliseconds")
            next_doc["punch_out"] = trunc_epoch_ms_to_dt(body.punch_out)
        if next_doc["punch_in"] is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for(next_doc["punch_in"], emp["shift_start"], emp["shift_end"]) != att_date:
            raise HTTPException(422, "punch_in must stay on the record attendance date")
        if next_doc["punch_out"] is not None:
            ensure_punch_out_window(next_doc["punch_in"], next_doc["punch_out"])

    next_doc.update(compute_derived(next_doc["status"], next_doc["punch_in"], next_doc["punch_out"], emp, att_date))
    comparable_fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in comparable_fields:
        default = False if field == "half_day" else 0 if field in {"late_minutes", "overtime_minutes"} else None
        before = rec.get(field, default)
        after = next_doc[field]
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}},
        {"$set": next_doc, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently")
    return scrub_doc(updated)


def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, **month_match(start, end)}},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}, {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]}]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    month, start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    row = rows[0] if rows else {}
    wd = working_days(emp["joined_on"], start, end)
    present_days = q2(row.get("present_days", 0))
    attendance_pct = None if wd == 0 else q4(Decimal(str(present_days)) / Decimal(wd) * Decimal(100))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def department_summary_pipeline(start: date, end: date, department: Optional[str]) -> list[dict]:
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}},
                    {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$lte": [{"$dayOfWeek": {"$ifNull": ["$logs.date_obj", {"$dateFromString": {"dateString": "1970-01-03"}}]}}, 6]}, {"$gte": [{"$dayOfWeek": {"$ifNull": ["$logs.date_obj", {"$dateFromString": {"dateString": "1970-01-03"}}]}}, 2]}]},
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
                "work_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$divide": ["$work_sum", "$work_count"]}, None]},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    month, start, end = parse_month(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(start, end, department)))
    for row in rows:
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = q2(row["avg_work_hours"])
        row["present_days"] = q2(row["present_days"])
    return {"month": month, "items": rows}


def late_leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]) -> list[dict]:
    pipeline = [
        {"$match": {**month_match(start, end), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    month, start, end = parse_month(month)
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": rows}


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    days = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$project": {
                "emp_code": 1,
                "joined_on": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "i",
                        "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "unit": "day", "amount": "$$i"}}}},
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "day": "$days"},
                "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}}],
                "as": "log",
            }
        },
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"day_obj": {"$dateFromString": {"dateString": "$days"}}}},
        {
            "$group": {
                "_id": "$days",
                "day_obj": {"$first": "$day_obj"},
                "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$days"]}, 1, 0]}},
                "present_count": {"$sum": {"$cond": [{"$in": ["$log.status", list(PRESENCE_STATUSES)]}, {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day_obj"}, 2]}, {"$lte": [{"$dayOfWeek": "$day_obj"}, 6]}]},
                "attendance_rate": {
                    "$cond": [
                        {"$and": [{"$gt": ["$headcount", 0]}, {"$gte": [{"$dayOfWeek": "$day_obj"}, 2]}, {"$lte": [{"$dayOfWeek": "$day_obj"}, 6]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start_s = validate_date_param(from_, "from")
    end_s = validate_date_param(to, "to")
    start, end = parse_date(start_s), parse_date(end_s)
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days > 91:
        raise HTTPException(422, "range must not exceed 92 days")
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    rows = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    for row in rows:
        row["present_count"] = q2(row["present_count"])
        if row["attendance_rate"] is not None:
            row["attendance_rate"] = q4(row["attendance_rate"])
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = q4(row["moving_avg_7d"])
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        date_from = validate_date_param(date_from, "date_from")
        date_to = validate_date_param(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            if status not in ALL_STATUSES:
                raise HTTPException(422, "invalid status")
            q["status"] = status
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": db.command("explain", cmd, verbosity="executionStats")}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        _, start, end = parse_month(month)
        return explain_aggregate(endpoint, "attendance_logs", employee_monthly_pipeline(emp_code, start, end))
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        _, start, end = parse_month(month)
        return explain_aggregate(endpoint, "employees", department_summary_pipeline(start, end, department))
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        _, start, end = parse_month(month)
        return explain_aggregate(endpoint, "attendance_logs", late_leaderboard_pipeline(start, end, limit, department))
    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        start_s = validate_date_param(from_, "from")
        end_s = validate_date_param(to, "to")
        start, end = parse_date(start_s), parse_date(end_s)
        if end < start or (end - start).days > 91:
            raise HTTPException(422, "invalid range")
        return explain_aggregate(endpoint, "employees", department_trend_pipeline(department, start, end))
    raise HTTPException(422, "unknown endpoint")


def explain_aggregate(endpoint: str, collection: str, pipeline: list[dict]) -> dict:
    cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": db.command("explain", cmd, verbosity="executionStats")}
