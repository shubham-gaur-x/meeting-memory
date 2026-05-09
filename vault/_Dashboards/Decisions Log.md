---
type: dashboard
---

# Decisions log

Every distinct decision called out in a meeting note's frontmatter, in reverse chronological order.

```dataview
TABLE WITHOUT ID
  decision AS "Decision",
  file.link AS "Meeting",
  date AS "Date",
  string(topics) AS "Topics"
FROM "Meetings"
FLATTEN decisions AS decision
WHERE type = "meeting"
SORT date DESC
```

## This quarter

```dataview
TABLE WITHOUT ID
  decision AS "Decision",
  file.link AS "Meeting",
  date AS "Date"
FROM "Meetings"
FLATTEN decisions AS decision
WHERE type = "meeting" AND date >= date(today) - dur(90 days)
SORT date DESC
```
