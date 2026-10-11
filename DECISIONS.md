# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` and compound `(department, joined_on)`. On `attendance_logs`, compound unique `(emp_code, date)` guarantees single-log constraints and race-safe punch-ins, `(date: -1, emp_code: 1)` optimizes cursor pagination, and `(emp_code, punch_in: -1)` accelerates overnight punch-outs. Index build failure aborts startup fast.

2. **Punch-in race.** Simultaneous duplicate punch-ins compute identical IST dates and insert. The first succeeds; the second trips unique `(emp_code, date)`, caught as `409 Conflict`. Regularization uses atomic compare-and-swap on `history` (matching missing history for legacy records), preventing lost audit logs.

3. **Ties.** Competition ranking (1, 2, 2, 4) uses `$setWindowFields` with `$rank` ordered by `total_late_minutes` descending. Applying rank cutoff after window evaluation retains all tied cutoff employees, sorted deterministically by `emp_code`.

4. **Headcount.** Rooted in `employees` filtering `joined_on <= month_end`, joining logs via `$lookup`. Zero-log employees and departments retain full headcount, reporting 0 present days and `null` average hours.

5. **100x Scale.** At 10M+ logs, in-memory grouping arrays risk MongoDB's 16MB document limit. We would maintain pre-aggregated daily summaries and monthly rollups via Change Streams, querying bounded rollups in single-digit milliseconds.
