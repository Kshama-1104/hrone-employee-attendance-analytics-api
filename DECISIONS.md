# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** We created `employees.emp_code` (unique) to guarantee employee code uniqueness under concurrent creation, and `(department, joined_on)` to accelerate department filtering and headcount aggregations. On `attendance_logs`, we created compound unique `(emp_code, date)` to enforce single-log-per-day constraints and race-safe punch-ins, `(date: -1, emp_code: 1)` for cursor sorting, `(emp_code, date, status)` for monthly lookups, `(date, status)` for cross-collection summarization, and `(emp_code, punch_in: -1)` for fast punch-out lookups across midnight. We considered and rejected an isolated index on `(emp_code, status)` because date-bounded queries make date the more selective compound prefix. Startup halts immediately if index creation fails to ensure traffic is never served without guarantees.

2. **Punch-in race.** Two identical punch-in requests arrive simultaneously. Both locate the employee and compute the same IST attendance date. Both attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` grants write lock to the first operation, returning 201. The concurrent request violates the unique constraint, raising `pymongo.errors.DuplicateKeyError`. Our endpoint catches this specific exception and returns HTTP 409 Conflict without race conditions or check-then-insert flaws.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.
