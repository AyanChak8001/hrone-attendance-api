"""Employee Attendance & Analytics API.

MongoDB stores instants as UTC BSON datetimes.  The API accepts and returns
epoch milliseconds, while attendance days and shifts use Indian Standard Time.
"""

# ---------------------------------------------------------------------------
# 1. Imports and configuration
# ---------------------------------------------------------------------------
import os
import re
from calendar import monthrange
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()


# ---------------------------------------------------------------------------
# 2. MongoDB connection and database setup
# ---------------------------------------------------------------------------
# Environment variables override values from .env. MongoClient opens connections
# lazily, so importing this module does not itself contact MongoDB.
def required_mongo_setting(name: str) -> str:
    """Return a required MongoDB setting without exposing its value in errors."""
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required configuration: {name}. Set it in the environment or .env.")
    return value


MONGO_URI = required_mongo_setting("MONGO_URI")
MONGO_DB = required_mongo_setting("MONGO_DB")
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]


# ---------------------------------------------------------------------------
# 3. Constants and attendance rules
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# 4. Shared helper functions
# ---------------------------------------------------------------------------
def bad(status_code: int, detail: str) -> None:
    """Raise a FastAPI error with the contract's normal {'detail': ...} body."""
    raise HTTPException(status_code, detail)


def utc(value: datetime) -> datetime:
    """Treat PyMongo's naive datetimes as UTC, or convert aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def mongo_dt(value: datetime) -> datetime:
    """Return a naive UTC datetime for PyMongo's default BSON codec."""
    return utc(value).replace(tzinfo=None)


def ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a MongoDB datetime to the API's integer epoch milliseconds."""
    if value is None:
        return None
    return int(utc(value).timestamp() * 1000)


def from_ms(value: int) -> datetime:
    """Convert request milliseconds to UTC and discard sub-second precision (R1)."""
    return datetime.fromtimestamp(value // 1000, timezone.utc).replace(microsecond=0)


def now() -> datetime:
    """Return the server time as a whole-second UTC instant."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def rnd(value: float, places: int) -> float:
    """Round with the assignment's required half-up rule, not Python's bankers rounding."""
    pattern = "1." + ("0" * places)
    return float(Decimal(str(value)).quantize(Decimal(pattern), rounding=ROUND_HALF_UP))


def day(value: str) -> Date:
    """Parse a YYYY-MM-DD string, returning 422 rather than an internal error."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        bad(422, "invalid date")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        bad(422, "invalid date")


def dates_for_month(value: str) -> tuple[Date, Date]:
    """Return the first and last calendar date for a validated YYYY-MM month."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        bad(422, "month must be YYYY-MM")
    try:
        year, month = map(int, value.split("-"))
        return Date(year, month, 1), Date(year, month, monthrange(year, month)[1])
    except ValueError:
        bad(422, "month must be YYYY-MM")


def serialize(value):
    """Recursively make a MongoDB document safe for the JSON API boundary."""
    if isinstance(value, datetime):
        return ms(value)
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        # MongoDB _id is internal and must never appear in responses.
        return {key: serialize(item) for key, item in value.items() if key != "_id"}
    return value


def out(document: dict) -> dict:
    """Normalise legacy attendance documents, then serialize BSON datetimes."""
    response = dict(document)
    defaults = {
        "history": [],
        "half_day": False,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "work_hours": None,
        "punch_in": None,
        "punch_out": None,
    }
    for field, default in defaults.items():
        response.setdefault(field, default)
    return serialize(response)


def attendance_day(punch: datetime, employee: dict) -> str:
    """Calculate the IST attendance date, including the overnight-shift exception (R1)."""
    local_punch = utc(punch).astimezone(IST)
    is_overnight = employee["shift_end"] <= employee["shift_start"]
    if is_overnight and local_punch.strftime("%H:%M") < employee["shift_end"]:
        return (local_punch.date() - timedelta(days=1)).isoformat()
    return local_punch.date().isoformat()


def shift_dt(date_value: str, clock: str, overnight: bool = False) -> datetime:
    """Build an IST datetime from an attendance date and an HH:MM shift clock."""
    hour, minute = map(int, clock.split(":"))
    shift_time = datetime.combine(day(date_value), datetime.min.time(), IST)
    shift_time = shift_time.replace(hour=hour, minute=minute)
    return shift_time + timedelta(days=1) if overnight else shift_time


def late(punch_in: datetime, employee: dict, attendance_date_value: str) -> int:
    """Calculate late minutes: strictly over ten minutes late, rounded down (R2)."""
    shift_start = shift_dt(attendance_date_value, employee["shift_start"])
    elapsed_seconds = (utc(punch_in) - shift_start.astimezone(timezone.utc)).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0


def overtime(punch_out: datetime, employee: dict, attendance_date_value: str) -> int:
    """Calculate overtime only when at least 30 whole minutes past shift end (R3)."""
    is_overnight = employee["shift_end"] <= employee["shift_start"]
    shift_end = shift_dt(attendance_date_value, employee["shift_end"], is_overnight)
    minutes = int((utc(punch_out) - shift_end.astimezone(timezone.utc)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived(
    employee: dict,
    attendance_date_value: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    status: str,
) -> dict:
    """Calculate the stored derived fields from final punch times and status (R2-R5)."""
    if status not in PRESENT:
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late_minutes = late(punch_in, employee, attendance_date_value)
    if punch_out is None:
        return {
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }

    work_hours = rnd((utc(punch_out) - utc(punch_in)).total_seconds() / 3600, 2)
    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime(punch_out, employee, attendance_date_value),
        "half_day": work_hours < 4.5,
    }


def db_values(values: dict) -> dict:
    """Convert only datetime values before writing a document to MongoDB."""
    return {
        key: mongo_dt(value) if isinstance(value, datetime) else value
        for key, value in values.items()
    }


def month_bounds(month: str) -> tuple[str, str, Date, Date]:
    """Return ISO-string and Date-object bounds used by monthly analytics."""
    first_day, last_day = dates_for_month(month)
    return first_day.isoformat(), last_day.isoformat(), first_day, last_day


def validate_attendance_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    """Validate the inclusive attendance-list date range used by both list and explain."""
    if date_from:
        day(date_from)
    if date_to:
        day(date_to)
    if date_from and date_to and date_from > date_to:
        bad(422, "date_from must not be after date_to")


def build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    """Build the one authoritative filter for attendance listing and its explain."""
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {
            **({"$gte": date_from} if date_from else {}),
            **({"$lte": date_to} if date_to else {}),
        }
    if status is not None:
        query["status"] = status
    return query


def validate_trend_range(from_: str, to: str) -> tuple[Date, Date]:
    """Validate the trend endpoint's inclusive date range and return parsed dates."""
    start, end = day(from_), day(to)
    if end < start:
        bad(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        bad(422, "range may not exceed 92 days")
    return start, end


# ---------------------------------------------------------------------------
# 5. Request models and startup indexes
# ---------------------------------------------------------------------------
class Model(BaseModel):
    """Ignore read-only/derived fields clients might send, as required by the contract."""

    model_config = ConfigDict(extra="ignore")


class EmployeeIn(Model):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, value: str) -> str:
        day(value)
        return value

    @model_validator(mode="after")
    def shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(Model):
    emp_code: str
    punched_at: Optional[Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def no_null_time(cls, value):
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(Model):
    emp_code: str
    punched_at: Optional[Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def no_null_time(cls, value):
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(Model):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]] = None
    punch_out: Optional[Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def no_null_punches(self):
        punch_in_was_sent = "punch_in" in self.model_fields_set
        punch_out_was_sent = "punch_out" in self.model_fields_set
        if (punch_in_was_sent and self.punch_in is None) or (punch_out_was_sent and self.punch_out is None):
            raise ValueError("punch times must be epoch milliseconds")
        return self


@app.on_event("startup")
def indexes():
    """Create the unique and query-supporting indexes idempotently at startup."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_natural_key",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_list",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("late_minutes", DESCENDING)],
        name="employee_month",
    )


# ---------------------------------------------------------------------------
# 6. Health check and employee endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Report ready only when MongoDB can answer a command."""
    try:
        db.command("ping")
    except Exception:
        bad(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee; the database unique index resolves concurrent duplicates."""
    employee = body.model_dump()
    employee["created_at"] = mongo_dt(now())
    try:
        db.employees.insert_one(employee)
    except DuplicateKeyError:
        bad(409, "emp_code already exists")
    return serialize(employee)


@app.get("/employees")
def employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """List one sorted page of employees and count only the filtered collection."""
    query = {} if department is None else {"department": department}
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize(employee) for employee in cursor],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 7. Attendance punch-in, punch-out, and listing
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """Create one attendance record for the employee's IST attendance day."""
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        bad(404, "employee not found")

    punch_time = from_ms(body.punched_at) if body.punched_at is not None else now()
    attendance_date_value = attendance_day(punch_time, employee)
    record = {
        "emp_code": body.emp_code,
        "date": attendance_date_value,
        "status": body.status,
        **derived(employee, attendance_date_value, punch_time, None, body.status),
        "history": [],
    }
    try:
        # The unique natural-key index makes duplicate punch-ins race-safe.
        db.attendance_logs.insert_one(db_values(record))
    except DuplicateKeyError:
        bad(409, "already punched in for this date")
    return out(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """Close the latest earlier punch-in with one conditional, race-safe update."""
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        bad(404, "employee not found")

    punch_time = from_ms(body.punched_at) if body.punched_at is not None else now()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": mongo_dt(punch_time)}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        bad(404, "no punch-in found")

    punch_in_time = utc(record["punch_in"])
    if punch_time <= punch_in_time or punch_time - punch_in_time > timedelta(hours=24):
        bad(422, "punch-out must be after punch-in and within 24 hours")

    final_values = derived(
        employee,
        record["date"],
        punch_in_time,
        punch_time,
        record["status"],
    )
    # Requiring punch_out=None means only one simultaneous request can win.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": db_values(final_values)},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        bad(409, "record already punched out")
    return out(updated)


@app.get("/attendance")
def attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """List filtered attendance records without loading the collection into Python."""
    validate_attendance_dates(date_from, date_to)
    query = build_attendance_list_query(emp_code, date_from, date_to, status)

    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [out(record) for record in cursor],
        "total": db.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 8. Attendance regularization and audit history
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    """Apply one manual correction and append an immutable before/after history entry."""
    day(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        bad(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        bad(404, "attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else record["status"]
    if final_status not in PRESENT and ("punch_in" in supplied or "punch_out" in supplied):
        bad(422, "ABSENT and LEAVE cannot have punch times")

    # PyMongo returns BSON datetimes as naive UTC values by default. Normalize
    # retained stored instants before comparing them with aware request values.
    final_punch_in = (
        from_ms(body.punch_in)
        if "punch_in" in supplied
        else utc(record["punch_in"]) if record.get("punch_in") is not None else None
    )
    final_punch_out = (
        from_ms(body.punch_out)
        if "punch_out" in supplied
        else utc(record["punch_out"]) if record.get("punch_out") is not None else None
    )

    if final_status not in PRESENT:
        final_punch_in = final_punch_out = None
    elif final_punch_in is None:
        bad(422, "a presence status requires punch_in")

    if final_punch_in is not None and attendance_day(final_punch_in, employee) != date:
        bad(422, "punch_in does not belong to this attendance date")
    if final_punch_out is not None:
        if final_punch_out <= utc(final_punch_in) or final_punch_out - utc(final_punch_in) > timedelta(hours=24):
            bad(422, "punch_out must be after punch-in and within 24 hours")

    final_values = db_values(
        {
            "status": final_status,
            **derived(
                employee,
                date,
                utc(final_punch_in) if final_punch_in else None,
                final_punch_out,
                final_status,
            ),
        }
    )
    legacy_defaults = {"half_day": False, "late_minutes": 0, "overtime_minutes": 0}
    tracked_fields = [
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ]
    changes = {
        field: {
            "from": record.get(field, legacy_defaults.get(field)),
            "to": final_values[field],
        }
        for field in tracked_fields
        if record.get(field, legacy_defaults.get(field)) != final_values[field]
    }
    if not changes:
        bad(422, "regularization changes nothing")

    history_entry = {
        "at": mongo_dt(now()),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # The history length is an optimistic concurrency check: a stale editor gets
    # 409 instead of replacing a correction another editor just appended.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {
                "$eq": [
                    {"$size": {"$ifNull": ["$history", []]}},
                    len(record.get("history", [])),
                ]
            },
        },
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        bad(409, "record changed concurrently")
    return out(updated)


# ---------------------------------------------------------------------------
# 9. Shared analytics helpers and monthly employee analytics
# ---------------------------------------------------------------------------
def workdays(first_day: Date, last_day: Date, joined_on: str) -> int:
    """Count Monday-Friday dates, starting at the later of month start and joining date."""
    first_day = max(first_day, day(joined_on))
    span = max(0, (last_day - first_day).days + 1)
    return sum((first_day + timedelta(days=offset)).weekday() < 5 for offset in range(span))


def is_weekday_expr(field: str = "$date") -> dict:
    """MongoDB expression that is true for Monday-Friday ISO date strings."""
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": field}}}
    return {"$and": [{"$gt": [weekday, 1]}, {"$lt": [weekday, 7]}]}


def present_expr() -> dict:
    """MongoDB expression that contributes 1, 0.5, or 0 present days."""
    return {
        "$cond": [
            {"$in": ["$status", list(PRESENT)]},
            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
            0,
        ]
    }


def mongo_half_up(expression: dict, places: int) -> dict:
    """Round a non-negative aggregation value half-up without MongoDB's tie-to-even $round."""
    scale = 10**places
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}},
            scale,
        ]
    }


def build_employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list[dict]:
    """Build the full monthly aggregation used both by the endpoint and explain."""
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [is_weekday_expr(), present_expr(), 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list[dict]:
    """Build the full employee-to-log department summary aggregation."""
    employee_match = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first]},
                                    {"$lte": ["$date", last]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "people": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    is_weekday_expr("$logs.date"),
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$people"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [{"$gt": [{"$size": "$work"}, 0]}, {"$avg": "$work"}, None]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    first: str,
    last: str,
    department: Optional[str],
    limit: int,
) -> list[dict]:
    """Build the full late-ranking pipeline, including rank-before-limit behavior."""
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    department: str,
    start: Date,
    end: Date,
) -> list[dict]:
    """Build one all-database trend pipeline with daily gap filling and a 7-day window."""
    start_at_utc_midnight = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_at_utc_midnight = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    end_exclusive = end_at_utc_midnight + timedelta(days=1)

    # A department has at least one employee because that is how departments are
    # represented in this data model. This seed makes $densify include start; its
    # exclusive upper bound makes the requested end date appear exactly once.
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "_trend_day": {"$literal": start_at_utc_midnight}}},
        {
            "$densify": {
                "field": "_trend_day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_at_utc_midnight, end_exclusive],
                },
            }
        },
        {"$match": {"_trend_day": {"$lte": end_at_utc_midnight}}},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$_trend_day",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "_eligible_employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "current_date": "$date",
                    "employee_codes": "$_eligible_employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$current_date"]},
                                    {"$in": ["$emp_code", "$$employee_codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {"$sum": present_expr()},
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "_daily_totals",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$_eligible_employees"},
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$_daily_totals.present_count", 0]}, 0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$_daily_totals.late_count", 0]}, 0]
                },
                "is_working_day": {
                    "$and": [
                        {"$gt": [{"$dayOfWeek": "$_trend_day"}, 1]},
                        {"$lt": [{"$dayOfWeek": "$_trend_day"}, 7]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_trend_day": 1},
                "output": {
                    "_moving_average_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
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
                        {"$ne": ["$_moving_average_raw", None]},
                        mongo_half_up("$_moving_average_raw", 4),
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    """Return one employee's monthly totals; MongoDB sums stored derived fields."""
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        bad(404, "employee not found")

    first, last, first_day, last_day = month_bounds(month)
    pipeline = build_employee_monthly_pipeline(emp_code, first, last)
    totals = next(db.attendance_logs.aggregate(pipeline), {})
    working_days = workdays(first_day, last_day, employee["joined_on"])
    present_days = totals.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": rnd(present_days / working_days * 100, 4) if working_days else None,
    }


# ---------------------------------------------------------------------------
# 10. Department summary, late leaderboard, and department trend analytics
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    """Group eligible employees by department, left-joining logs so zero-log staff count."""
    first, last, _, _ = month_bounds(month)
    pipeline = build_department_summary_pipeline(first, last, department)
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = rnd(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def leaderboard(
    month: str,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    """Rank late employees with MongoDB's standard competition rank window function."""
    first, last, _, _ = month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(first, last, department, limit)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
):
    """Return daily department data using MongoDB gap filling and window calculations."""
    start, end = validate_trend_range(from_, to)

    # The data model has no separate departments collection: no employee means
    # the department is unknown and must produce the contract's 404.
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        bad(404, "department not found")

    pipeline = build_department_trend_pipeline(department, start, end)
    return {"department": department, "items": list(db.employees.aggregate(pipeline))}


# ---------------------------------------------------------------------------
# 11. Admin explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Explain the same main find or aggregation operation the named endpoint runs."""
    if endpoint == "attendance_list":
        validate_attendance_dates(date_from, date_to)
        query = build_attendance_list_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            bad(422, "emp_code and month are required")
        first, last, _, _ = month_bounds(month)
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": build_employee_monthly_pipeline(emp_code, first, last),
            "cursor": {},
        }
    elif endpoint == "department_summary":
        if not month:
            bad(422, "month is required")
        first, last, _, _ = month_bounds(month)
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": build_department_summary_pipeline(first, last, department),
            "cursor": {},
        }
    elif endpoint == "late_leaderboard":
        if not month:
            bad(422, "month is required")
        first, last, _, _ = month_bounds(month)
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": build_late_leaderboard_pipeline(first, last, department, limit),
            "cursor": {},
        }
    else:
        if not department or not from_ or not to:
            bad(422, "department, from and to are required")
        start, end = validate_trend_range(from_, to)
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": build_department_trend_pipeline(department, start, end),
            "cursor": {},
        }

    # MongoDB supports explain with executionStats for read-only find and
    # aggregate commands. This returns the server's real raw plan, never a
    # locally fabricated approximation.
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": db.command("explain", command, verbosity="executionStats"),
    }
