"""Employee Attendance & Analytics API."""
import os
import re
import json
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")


@app.on_event("startup")
def startup():
    indexes()


def epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def from_ms(value: int) -> datetime:
    if type(value) is not int or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise ValueError("must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def trunc(dt: datetime) -> datetime:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError
        return d
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")


def shift_dt(day: date, shift: str) -> datetime:
    h, m = map(int, shift.split(":"))
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def attendance_day(punch_in: datetime, emp: dict) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day


def late_minutes(punch: datetime, emp: dict) -> int:
    day = attendance_day(punch, emp)
    start = shift_dt(day, emp["shift_start"])
    elapsed = int((punch - start).total_seconds() // 60)
    return elapsed if (punch - start).total_seconds() > 600 else 0


def metrics(pin: datetime | None, pout: datetime | None, emp: dict) -> dict:
    if pin is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pin, emp)
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    secs = int((pout - pin).total_seconds())
    hours = float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    day = attendance_day(pin, emp)
    end_day = day + (timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else timedelta())
    end = shift_dt(end_day, emp["shift_end"])
    overtime = int((pout - end).total_seconds() // 60)
    if overtime < 30: overtime = 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}


def clean_doc(d: dict) -> dict:
    d = dict(d); d.pop("_id", None)
    for k in ("punch_in", "punch_out", "created_at"):
        if isinstance(d.get(k), datetime): d[k] = epoch(d[k])
    if "half_day" not in d: d["half_day"] = False
    d.setdefault("history", [])
    for h in d.get("history", []):
        if isinstance(h.get("at"), datetime): h["at"] = epoch(h["at"])
        for change in h.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime): change[side] = epoch(change[side])
    return d


def date_filter(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-")); last = monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def round_num(n, places=2):
    if n is None: return None
    return float(Decimal(str(n)).quantize(Decimal("1." + "0" * places), rounding=ROUND_HALF_UP))


def stages_explain(stages):
    # PyMongo's explain command accepts an aggregate command with executionStats verbosity.
    return db.command("explain", {"aggregate": "attendance_logs", "pipeline": stages, "cursor": {}}, verbosity="executionStats")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v): parse_date(v); return v
    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end: raise ValueError("shift times must differ")
        return self

class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None: from_ms(v)
        return v

class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None: from_ms(v)
        return v

class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, v):
        if v is not None: from_ms(v)
        return v
    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


@app.get("/health")
def health():
    try: db.command("ping"); return {"status": "ok"}
    except Exception: raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = datetime.now(UTC)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return clean_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    return {"items": [clean_doc(x) for x in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)], "total": db.employees.count_documents(q), "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = trunc(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    d = attendance_day(ts, emp).isoformat()
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None, "work_hours": None, **metrics(ts, None, emp), "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return clean_doc(doc)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    cur = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [clean_doc(d) for d in cur], "total": db.attendance_logs.count_documents(q), "page": page, "page_size": page_size}

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = trunc(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "status": {"$in": PRESENCE}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        later = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "status": {"$in": PRESENCE}, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if later and ts <= later["punch_in"]:
            raise HTTPException(422, "punch-out must be after punch-in")
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts > rec["punch_in"] + timedelta(hours=24): raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    vals = metrics(rec["punch_in"], ts, emp)
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None: raise HTTPException(409, "already punched out")
    return clean_doc(result)

@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    parse_date(day)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not old: raise HTTPException(404, "attendance record not found")
    pin = old.get("punch_in"); pout = old.get("punch_out"); status = old["status"]
    if "status" in body.model_fields_set: status = body.status
    if "punch_in" in body.model_fields_set: pin = from_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in body.model_fields_set: pout = from_ms(body.punch_out) if body.punch_out is not None else None
    if status in ("ABSENT", "LEAVE"):
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None): raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        pin = pout = None
    elif pin is None: raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and attendance_day(pin, emp).isoformat() != day: raise HTTPException(422, "punch_in must fall on attendance date")
    if pout is not None and (pin is None or pout <= pin or pout > pin + timedelta(hours=24)): raise HTTPException(422, "invalid punch_out")
    vals = metrics(pin, pout, emp)
    new = {"status": status, "punch_in": pin, "punch_out": pout, **vals}
    changed = {}
    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False, "work_hours": None}
    for key, value in new.items():
        before = old[key] if key in old else defaults.get(key)
        if before != value: changed[key] = {"from": before, "to": value}
    if not changed: raise HTTPException(422, "request makes no changes")
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    # Optimistic compare-and-set prevents either update from overwriting the other's history.
    selector = {"_id": old["_id"]}
    selector["history"] = old["history"] if "history" in old else {"$exists": False}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        selector[k] = old[k] if k in old else {"$exists": False}
    result = db.attendance_logs.find_one_and_update(
        selector,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None: raise HTTPException(409, "record changed concurrently; retry")
    return clean_doc(result)


def month_bounds(month: str):
    start, end = date_filter(month)
    return start, end


def weekday_presence(status_field: str, half_day_field: str, date_field: str):
    """Return a Mongo expression for weekday presence, including half days."""
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}
    is_weekday = {"$and": [{"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]}
    present_value = {"$cond": [{"$eq": [{"$ifNull": [half_day_field, False]}, True]}, 0.5, 1]}
    return {"$cond": [{"$and": [{"$in": [status_field, list(PRESENCE)]}, is_weekday]}, present_value, 0]}


def employee_monthly_pipeline(emp_code: str, month: str):
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": weekday_presence("$status", "$half_day", "$date")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pipeline = employee_monthly_pipeline(emp_code, month)
    row = next(db.attendance_logs.aggregate(pipeline), {})
    joined = max(parse_date(emp["joined_on"]), date.fromisoformat(start))
    end_d = date.fromisoformat(end)
    working = sum(1 for i in range((end_d-joined).days+1) if (joined+timedelta(days=i)).weekday() < 5) if joined <= end_d else 0
    return {"emp_code": emp_code, "month": month, "working_days": working,
        "present_days": round_num(row.get("present_days", 0)), "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": round_num(row.get("present_days", 0) / working * 100, 2) if working else None}


def department_summary_pipeline(month: str, department: str | None):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": weekday_presence("$logs.status", "$logs.half_day", "$logs.date")},
            "work_total": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$divide": ["$work_total", "$work_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"department": 1}}
    ]

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    pipeline = department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))
    for row in rows:
        row["present_days"] = round_num(row["present_days"])
        row["avg_work_hours"] = round_num(row["avg_work_hours"])
    return {"month": month, "items": rows}


def leaderboard_pipeline(month: str, department: str | None, limit: int = 50):
    start, end = month_bounds(month)
    pipe = [{"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None: pipe.append({"$match": {"employee.department": department}})
    pipe += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return pipe

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    pipe = leaderboard_pipeline(month, department, limit)
    rows = list(db.attendance_logs.aggregate(pipe))
    return {"month": month, "items": rows}


def trend_pipeline(department: str, start: str, end: str):
    # Generate a complete calendar in MongoDB, then indexed lookups attach logs and active employees.
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    count = (end_d - start_d).days + 1
    return [
        {"$documents": [{"start": start, "count": count}]},
        {"$project": {"dates": {"$map": {"input": {"$range": [0, "$count"]}, "as": "i", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": "$start"}}, "unit": "day", "amount": "$$i"}}, "format": "%Y-%m-%d"}}}}}},
        {"$unwind": "$dates"}, {"$project": {"date": "$dates"}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "daylog"}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "present_count": {"$ifNull": [{"$arrayElemAt": ["$daylog.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daylog.late_count", 0]}, 0]}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}, {"$sort": {"date": 1}}
    ]

def validate_trend_range(from_: str, to: str):
    start_d, end_d = parse_date(from_), parse_date(to)
    if end_d < start_d or (end_d-start_d).days + 1 > 92: raise HTTPException(422, "invalid date range")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validate_trend_range(from_, to)
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    pipe = trend_pipeline(department, from_, to)
    # $documents is a database-level source stage in MongoDB 6.0+.
    rows = list(db.aggregate(pipe))
    for row in rows:
        row["attendance_rate"] = round_num(row["attendance_rate"], 4)
        row["moving_avg_7d"] = round_num(row["moving_avg_7d"], 4)
        row["present_count"] = round_num(row["present_count"])
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from)
        if date_to: parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}
        collection = "attendance_logs"
        explain = db.command("explain", cmd, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        pipe = employee_monthly_pipeline(emp_code, month)
        collection = "attendance_logs"; explain = stages_explain(pipe)
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        pipe = department_summary_pipeline(month, department)
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, verbosity="executionStats"); collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        pipe = leaderboard_pipeline(month, department, limit)
        explain = stages_explain(pipe); collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        validate_trend_range(from_, to)
        pipe = trend_pipeline(department, from_, to)
        explain = db.command("explain", {"aggregate": 1, "pipeline": pipe, "cursor": {}}, verbosity="executionStats"); collection = "$database"
    else: raise HTTPException(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explain))}
