# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** `employees.emp_code` is unique. Attendance has a unique `(emp_code, date)` natural key, a `(date desc, emp_code)` listing index, and `(emp_code, date, late_minutes)` for employee-month queries. Employee department/joined and joined-on indexes support headcount. I did not add separate single-field status indexes: they are low-selectivity and do not serve the main sorts.
2. **Punch-in race.** Both requests attempt the insert. MongoDB admits one natural key and rejects the other with its duplicate-key error, which the API maps to 409. There is no check-then-insert race.
3. **Ties.** The leaderboard uses MongoDB rank, then filters ranks at the requested limit. Everyone tied at the cutoff rank is returned, in emp-code order within equal totals.
4. **Headcount.** The department summary begins with eligible employee documents and left-joins their logs. Thus an employee with no matching log still contributes one member to the headcount set.
5. **At 100x scale** I would pre-aggregate immutable daily/department metrics and refresh a small materialized collection after a correction, reducing the expensive month-wide joins.
