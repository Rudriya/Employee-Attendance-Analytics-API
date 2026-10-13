1. **Indexes.** `emp_code` is unique in both collections: employees have one business identity, and attendance has one row per employee per day. The employee index on `(department, joined_on)` supports department headcount; a separate `joined_on` index supports the all-department summary. The attendance index on `(date, emp_code)` supports date filtering and the required list order. I considered a standalone `status` index but left it out because most records share a status and the date index already supports the list query.

2. **Punch-in race.** Both requests try the insert. The unique attendance index lets one succeed and makes MongoDB reject the other; that request becomes a 409.

3. **Ties.** The leaderboard uses `$rank`, so a tie at the limit is returned in full and the following rank is skipped. Employee code orders people within the tie.

4. **Headcount.** The department summary starts with employees who have joined by month end, then looks up their logs. Keeping employees with no match in the result counts people with no attendance records.

5. **100x data.** I would first check the real query plans and traffic. If analytics became the bottleneck, I would try daily summary records and refresh them as attendance changes.
