# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned 200 without checking MongoDB. | Stop MongoDB and `GET /health` still said ok. | Added `db.command("ping")` and return 503 on failure. |
| 2 | startup | No indexes were created. | Hidden DB starts with only `_id`; duplicate races and large queries are slow. | Added idempotent unique and query-supporting indexes at startup. |
| 3 | `create_employee` | Duplicate check was check-then-insert, so concurrent creates could both pass the check. | Parallel `POST /employees` with the same `emp_code`. | Rely on the unique index and translate `DuplicateKeyError` to 409. |
| 4 | `create_employee` | `created_at` was a naive `datetime` and returned as a datetime, not epoch milliseconds. | Contract test expects integer milliseconds. | Store UTC BSON datetimes and serialize datetimes to epoch milliseconds at the boundary. |
| 5 | models | Inputs were under-validated: employee code, email, dates, shift times, statuses and epoch milliseconds. | Seconds-looking timestamps or bad statuses were accepted. | Added Pydantic validators and FastAPI query constraints. |
| 6 | `list_employees` | Pagination skipped `page * page_size`, so page 1 skipped the first page. | `GET /employees?page=1&page_size=20` starts at row 21. | Changed skip to `(page - 1) * page_size`. |
| 7 | `list_employees` | `total` ignored the department filter. | `GET /employees?department=Sales` returned total for all departments. | Count the filtered query. |
| 8 | `list_employees` | Results were not sorted by `emp_code`. | Records appeared in insertion order. | Added `sort("emp_code", ASCENDING)`. |
| 9 | `punch_in` | Unknown employees were not handled; `emp["shift_start"]` could crash with 500. | Punch in with a missing `emp_code`. | Return 404 before computing the record. |
| 10 | `punch_in` | Attendance date used the server-local date, not IST/R1 overnight rules. | Overnight shift punch at 05:00 IST gets the wrong date. | Compute the attendance date from the punch instant, IST, and shift boundaries. |
| 11 | `punch_in` | Duplicate punch-in was check-then-insert and not race safe. | Two simultaneous requests could both create records. | Use the unique `(emp_code, date)` index and catch duplicate insert errors. |
| 12 | `compute_late_minutes` | Used `replace()` on the punch datetime and did not handle IST/overnight shift boundaries. | Shift start comparison is wrong for UTC-stored datetimes and overnight shifts. | Calculate shift start as an IST local boundary converted to UTC. |
| 13 | `compute_work_hours` | Used Python `round`, which is banker's rounding, not half-up. | Values ending in `.005` round incorrectly. | Use `Decimal(...).quantize(..., ROUND_HALF_UP)`. |
| 14 | `compute_overtime` | Counted any positive overtime and did not handle overnight shift ends. | 5 minutes overtime was returned as 5; overnight shifts used same-day end. | Apply the 30-minute threshold and next-day end for overnight shifts. |
| 15 | `list_attendance` | Loaded all matching docs into memory, sorted in Python and then sliced. | Slow or memory-heavy on 100k records. | Use indexed MongoDB sort/skip/limit and `count_documents`. |
| 16 | `list_attendance` | Returned Mongo `_id` as `id`, but attendance records have no API id. | Response includes an unexpected field. | Remove `_id` from all API responses. |
| 17 | `list_attendance` | Did not validate date ranges or status. | `date_from > date_to` returned an empty page instead of 422. | Added contract validation. |
| 18 | helpers/responses | Missing seeded fields `history` and `half_day` were not normalized. | Legacy sample document omits them. | Serializer defaults missing `history` to `[]` and `half_day` to `false`. |

Also checked: using `emp_code` instead of MongoDB `_id` as the public identifier is correct per the data model, and storing calendar dates as strings is intentional because attendance dates are IST calendar values, not instants.
