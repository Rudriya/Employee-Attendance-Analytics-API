# Starter code review

| # | Where | Defect | How it shows up | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds shift start on the punch-in's calendar date, so an early-morning overnight punch gets compared with a start later that day. | 02:00 punch-in for a 22:00–06:00 shift is treated as not late. | Determine the R1 attendance day first and build the start instant from it. |
| 2 | `compute_late_minutes` | Does not truncate instants to seconds; its `int()` also truncates toward zero. | For a 09:30 shift, 09:40:00.900 incorrectly passes the grace period. | Truncate first; apply the strict 600-second check and floor full minutes from shift start. |
| 3 | `compute_work_hours` | Python `round()` uses ties-to-even, not the contract's half-up rule. | A work duration on a half-cent boundary can round down. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Uses the punch-in date for shift end even on an overnight shift. | A 22:00–06:00 shift ending at 06:40 is compared with 06:00 on the previous day. | Put the overnight shift end on the next date. |
| 5 | `compute_overtime` | Counts any positive overtime; R3 requires at least 30 minutes. | An 18:35 punch-out for an 18:30 shift reports 5 minutes. | Return zero below the threshold. |
| 6 | `EmployeeIn` | Does not validate the employee-code format, field limits, date/time formats, or unequal shift times. | Submit a malformed code/date, oversized name, or identical shifts. | Add the contract's Pydantic constraints and date/shift checks. |
| 7 | `/health` | Always returns 200 without contacting MongoDB. | Stop MongoDB and call the readiness endpoint. | Ping MongoDB and return 503 on failure. |
| 8 | `create_employee` | Duplicate check followed by insert is racy, and there is no unique index. | Parallel creates can both pass the check. | Enforce uniqueness in MongoDB and map duplicate-key errors to 409. |
| 9 | `create_employee` | Uses naive local time for `created_at` and returns a datetime string instead of epoch milliseconds. | Inspect the stored instant on a non-UTC host or compare the response type with the contract. | Store UTC and serialize as epoch milliseconds. |
| 10 | `list_employees` | Skips `page * page_size`, so page 1 starts after the first page. | Request page 1 with several employees. | Skip `(page - 1) * page_size`. |
| 11 | `list_employees` | Counts all employees instead of the filtered set and does not sort by `emp_code`. | Filter by department or repeat a paginated request. | Apply the same filter to count and results, and sort in MongoDB. |
| 12 | `punch_in` | Does not handle an unknown employee before reading its shift. | Unknown `emp_code` causes a 500 instead of 404. | Check for the employee and return 404. |
| 13 | `punch_in` | Uses local-time `fromtimestamp`, treats timestamp zero as “not supplied,” and accepts seconds/floats without validation or second truncation. | Try a seconds-looking value, a float, or a punch near the IST date boundary. | Validate epoch milliseconds, convert in UTC, and truncate before deriving the date. |
| 14 | `punch_in` | Does not restrict status to the three punchable statuses and relies on check-then-insert for one-per-day. | Send `ABSENT`, or issue simultaneous same-day requests. | Validate the enum; use a unique `(emp_code, date)` index and catch duplicate-key errors. |
| 15 | `list_employees`, `list_attendance` | Page and page size have no lower/upper bounds from R10. | Try page 0 or page size 101. | Validate query parameters. |
| 16 | `list_attendance` | Loads and sorts every match in Python, so it scales poorly; it also adds an `id` field even though attendance has no API id. | Use a broad filter on 100k records or inspect a response. | Sort, count, skip, and limit in MongoDB; omit `_id` and do not add `id`. |
| 17 | `list_attendance` | Does not validate date strings/order or status values. | Submit an invalid date, reversed range, or unknown status. | Validate the filters and return 422 for invalid inputs. |

**Checked and kept:** elapsed-time subtraction in `compute_work_hours` is appropriate for durations, `load_dotenv()` leaves real environment variables in control by default, and `PRESENT` is the correct default punch status.
