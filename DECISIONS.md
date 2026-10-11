# DECISIONS.md

1. **Indexes.** I create unique indexes on `employees.emp_code` and `(attendance_logs.emp_code, date)` for identity and race safety. I also index employee department/join date for headcount queries, attendance date/employee/status for lists and summaries, employee/punch-in for punch-out, and date/late minutes for the leaderboard. I considered a separate `status`-only index but rejected it because the contract filters status with date and employee more often than alone.
2. **Punch-in race.** Both requests compute the same attendance date and try the same insert. MongoDB's unique `(emp_code, date)` index accepts one write. The loser raises `DuplicateKeyError`, which the API returns as 409.
3. **Ties.** The leaderboard uses MongoDB `$rank`, so tied employees share the same rank and the next rank is skipped. `limit` is applied to rank, not row count, so everyone tied at a rank within the cutoff is returned.
4. **Headcount.** Department summary starts from `employees`, filtered by `joined_on <= month_end`, then looks up logs. That means employees with zero logs are still grouped and counted.
5. **100x data.** I would pre-aggregate daily employee facts into monthly/department rollups and serve analytics from those rollups, while keeping raw attendance logs for audit and drill-down.
