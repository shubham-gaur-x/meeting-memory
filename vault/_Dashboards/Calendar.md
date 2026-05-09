---
type: dashboard
---

# Calendar view

Group meetings by day, newest first.

```dataview
TABLE WITHOUT ID
  dateformat(date, "EEE, dd LLL yyyy") AS "Day",
  length(rows) AS "Meetings",
  sum(rows.duration_minutes) AS "Minutes"
FROM "Meetings"
WHERE type = "meeting"
GROUP BY date
SORT date DESC
```

## Today

```dataview
LIST WITHOUT ID
  file.link + "  (" + (start_time + " · " + platform) + ")"
FROM "Meetings"
WHERE type = "meeting" AND date = date(today)
SORT start_time ASC
```

## Tomorrow

```dataview
LIST WITHOUT ID
  file.link + "  (" + (start_time + " · " + platform) + ")"
FROM "Meetings"
WHERE type = "meeting" AND date = date(today) + dur(1 day)
SORT start_time ASC
```
