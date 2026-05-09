---
type: dashboard
---

# Meeting Memory — Home

Welcome. This vault is auto-populated by `ingest.py` from your Gmail. Open these dashboards:

- [[_Dashboards/Action Items]]
- [[_Dashboards/Decisions Log]]
- [[_Dashboards/People]]
- [[_Dashboards/Topics]]
- [[_Dashboards/Weekly Digest]]
- [[_Dashboards/Calendar]]

## Last 14 days

```dataview
TABLE WITHOUT ID
  file.link AS "Meeting",
  date AS "Date",
  platform AS "Where",
  length(attendees) AS "#",
  string(topics) AS "Topics"
FROM "Meetings"
WHERE type = "meeting" AND date >= date(today) - dur(14 days)
SORT date DESC
LIMIT 50
```

## Pending follow-ups

```dataview
LIST file.link
FROM "Meetings"
WHERE type = "meeting" AND follow_up_needed = true
SORT date DESC
```

## Coverage

```dataview
TABLE WITHOUT ID
  length(rows) AS "Meetings",
  min(rows.date) AS "First",
  max(rows.date) AS "Latest"
FROM "Meetings"
WHERE type = "meeting"
GROUP BY ""
```
