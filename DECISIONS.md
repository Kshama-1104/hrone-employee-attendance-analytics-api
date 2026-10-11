# DECISIONS.md

1. **Indexes.** We created unique `employees.emp_code` and compound `(department, joined_on)`. On `attendance_logs`, compound unique `(emp_code, date)` guarantees single-log constraints and race-safe punch-ins, `(date: -1, emp_code: 1)` optimizes cursor pagination, and `(emp_code, punch_in: -1)` accelerates overnight punch-outs. We rejected indexing `(emp_code, status)` alone because date prefixes provide superior selectivity. App startup fails fast if index builds fail.

2. **Punch-in race.** Simultaneous duplicate punch-ins compute identical IST dates and attempt `insert_one`. The first succeeds; the second trips the unique `(emp_code, date)` index. The API catches `DuplicateKeyError` and returns `409 Conflict`, avoiding race-prone check-then-insert patterns. Regularization uses atomic compare-and-swap on history (matching `history: {$exists: false}` for legacy records), preventing lost audit entries.

3. **Ties.** Ranking uses MongoDB's `$setWindowFields` with `$rank` ordered strictly by `total_late_minutes` descending (standard competition ranking: 1, 2, 2, 4). Applying the rank cutoff *after* window computation retains all tied employees at the boundary (exceeding `limit` when ties occur), sorted deterministically by `emp_code`.

4. **Headcount.** The pipeline roots in `employees`, filtering `joined_on <= month_end`. Employees join attendance logs via `$lookup`. Zero-log employees and departments retain full headcount, reporting 0 present days and `null` average hours rather than vanishing.

5. **100x Scale Change.** At 10M+ logs, the trend pipeline grouping department employees into an in-memory array risks MongoDB's 16MB document limit. We would maintain pre-aggregated daily department summaries via Change Streams, querying bounded rollups in single-digit milliseconds instead of costly multi-collection raw aggregations.

