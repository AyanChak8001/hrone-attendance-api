from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from fastapi import HTTPException

# Unit tests import the application directly; keep that import isolated from
# both production databases while still allowing callers to provide values.
os.environ.setdefault("MONGO_URI", "mongodb://[::1]:27017")
os.environ.setdefault("MONGO_DB", "attendance_test_unit")

import app.main as main
from app.main import EmployeeIn, PunchInIn, PunchOutIn, RegularizeIn, attendance_day, day, dates_for_month, derived, month_bounds, required_mongo_setting


EMP = {
    "emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com",
    "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-01-01",
}


class _Employees:
    def __init__(self, employee): self.employee = employee
    def find_one(self, query): return self.employee if self.employee and query == {"emp_code": self.employee["emp_code"]} else None


class _Logs:
    def __init__(self, record): self.record = record
    def find_one(self, query, sort=None):
        if self.record is None: return None
        if "punch_in" in query:
            return self.record if self.record["emp_code"] == query["emp_code"] and self.record["punch_in"] <= query["punch_in"]["$lte"] else None
        return self.record if self.record["emp_code"] == query["emp_code"] and self.record["date"] == query["date"] else None
    def find_one_and_update(self, query, update, return_document=None):
        if self.record is None or ("punch_out" in query and self.record.get("punch_out") is not None): return None
        self.record.update(update["$set"])
        return self.record


class _DB:
    def __init__(self, employee, record): self.employees, self.attendance_logs = _Employees(employee), _Logs(record)


class AttendanceRulesTests(unittest.TestCase):
  def setUp(self):
    self.old_db = main.db

  def tearDown(self):
    main.db = self.old_db

  def test_punch_out_success_and_second_punch_is_conflict(self):
    record = {"_id": 1, "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
              "punch_in": datetime(2026, 7, 6, 4, 0), "punch_out": None, "history": []}
    main.db = _DB(EMP, record)
    response = main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=1783342800000))  # 18:30 IST
    self.assertEqual(response["work_hours"], 9.0)
    self.assertEqual(response["punch_out"], 1783342800000)
    with self.assertRaises(HTTPException) as error:
      main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=1783342800000))
    self.assertEqual(error.exception.status_code, 409)

  def test_punch_out_unknown_employee_and_missing_regularization_record_are_404(self):
    main.db = _DB(None, None)
    with self.assertRaises(HTTPException) as error:
      main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=1783345200000))
    self.assertEqual(error.exception.status_code, 404)
    main.db = _DB(EMP, None)
    with self.assertRaises(HTTPException) as error:
      main.regularize("EMP0001", "2026-07-06", RegularizeIn(reason="valid reason", regularized_by="hr", status="ABSENT"))
    self.assertEqual(error.exception.status_code, 404)
  def test_employee_validation_rejects_equal_shift_times(self):
    with self.assertRaises(ValidationError):
        EmployeeIn(**{**EMP, "shift_end": "09:30"})


  def test_employee_validation_accepts_codes_and_24_hour_shift_times(self):
    employee = EmployeeIn(**{**EMP, "email": "ada@example.com", "emp_code": "EMP123456", "shift_start": "00:00", "shift_end": "23:59"})
    self.assertEqual(employee.emp_code, "EMP123456")
    self.assertEqual(employee.shift_start, "00:00")
    self.assertEqual(employee.shift_end, "23:59")

    for field, value in (("emp_code", "EMP123"), ("shift_start", "24:00"), ("shift_start", "09:60"), ("shift_end", "9:30")):
      with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
        EmployeeIn(**{**EMP, "email": "ada@example.com", field: value})


  def test_employee_email_validation_accepts_valid_addresses(self):
    for email in ("asha@example.com", "susan.Smith@example.com", "user@mail.example.com"):
      with self.subTest(email=email):
        self.assertEqual(EmployeeIn(**{**EMP, "email": email}).email, email)


  def test_employee_email_validation_rejects_malformed_addresses(self):
    for email in ("user.example.com", "user@examplecom", "user name@example.com"):
      with self.subTest(email=email), self.assertRaises(ValidationError):
        EmployeeIn(**{**EMP, "email": email})


  def test_punch_time_requires_integer_epoch_milliseconds(self):
    for model in (PunchInIn, PunchOutIn):
      for value in (99999999999, 1783312500, 1783312500000.0, None):
        with self.subTest(model=model, value=value), self.assertRaises(ValidationError):
          model(emp_code="EMP0001", punched_at=value)


  def test_omitted_punch_time_is_allowed(self):
    self.assertIsNone(PunchInIn(emp_code="EMP0001").punched_at)
    self.assertIsNone(PunchOutIn(emp_code="EMP0001").punched_at)


  def test_late_grace_half_up_work_and_overtime_rules(self):
    emp = {**EMP}
    pin = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST
    pout = datetime(2026, 7, 6, 13, 40, tzinfo=timezone.utc)  # 19:10 IST
    values = derived(emp, "2026-07-06", pin, pout, "PRESENT")
    self.assertEqual(values["late_minutes"], 10)
    self.assertEqual(values["work_hours"], 9.5)
    self.assertEqual(values["overtime_minutes"], 40)
    self.assertIs(values["half_day"], False)


  def test_overnight_early_morning_punch_belongs_to_previous_day(self):
    emp = {**EMP, "shift_start": "22:00", "shift_end": "06:00"}
    pin = datetime(2026, 7, 7, 0, 20, tzinfo=timezone.utc)  # 05:50 IST
    self.assertEqual(attendance_day(pin, emp), "2026-07-06")


  def test_regularization_rejects_null_punch_and_short_reason(self):
    with self.assertRaises(ValidationError):
        RegularizeIn(reason="valid reason", regularized_by="hr", punch_in=None)
    with self.assertRaises(ValidationError):
        RegularizeIn(reason="no", regularized_by="hr")


  def test_invalid_month_is_rejected(self):
    self.assertEqual(dates_for_month("2026-07")[0].isoformat(), "2026-07-01")
    for value in ("2026-00", "2026-13", "2026-1", "123-001", "2026/07", "bad"):
      with self.subTest(value=value), self.assertRaises(Exception):
        month_bounds(value)


  def test_mongo_configuration_rejects_missing_and_empty_values(self):
    for name in ("MONGO_URI", "MONGO_DB"):
      with self.subTest(name=name):
        with patch.dict(os.environ, {}, clear=False):
          os.environ.pop(name, None)
          with self.assertRaisesRegex(RuntimeError, name):
            required_mongo_setting(name)
        with patch.dict(os.environ, {name: "   "}, clear=False):
          with self.assertRaisesRegex(RuntimeError, name):
            required_mongo_setting(name)


  def test_process_environment_value_wins_for_mongo_configuration(self):
    with patch.dict(os.environ, {"MONGO_URI": "mongodb://process.example", "MONGO_DB": "process_db"}, clear=False):
      self.assertEqual(required_mongo_setting("MONGO_URI"), "mongodb://process.example")
      self.assertEqual(required_mongo_setting("MONGO_DB"), "process_db")


  def test_date_validation_requires_exact_shape_and_real_calendar_date(self):
    self.assertEqual(day("2026-07-11").isoformat(), "2026-07-11")
    for value in ("2026/07/11", "20260711", "2026-W28-6", "2026-02-30", "26-07-11", "12345-07-11"):
      with self.subTest(value=value), self.assertRaises(HTTPException) as error:
        day(value)
      self.assertEqual(error.exception.status_code, 422)
