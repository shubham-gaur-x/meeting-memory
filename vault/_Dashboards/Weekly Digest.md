---
type: dashboard
---

# Weekly digest

Rolling view of the last 7 days. Pin this tab.

## Meetings this week

```dataview
TABLE WITHOUT ID
  file.link AS "Meeting",
  date AS "Date",
  platform AS "Where",
  string(topics) AS "Topics",
  length(decisions) AS "#dec",
  length(action_items) AS "#act"
FROM "Meetings"
WHERE type = "meeting" AND date >= date(today) - dur(7 days)
SORT date DESC
```

## Decisions made this week

```dataview
LIST WITHOUT ID
  decision + "  —  " + file.link + "  (" + dateformat(date, "yyyy-MM-dd") + ")"
FROM "Meetings"
FLATTEN decisions AS decision
WHERE type = "meeting" AND date >= date(today) - dur(7 days)
SORT date DESC
```

## Action items raised this week

```dataview
TABLE WITHOUT ID
  item.owner AS "Owner",
  item.task AS "Task",
  item.due AS "Due",
  file.link AS "Meeting"
FROM "Meetings"
FLATTEN action_items AS item
WHERE type = "meeting" AND date >= date(today) - dur(7 days)
SORT item.due ASC
```

## Meeting load

```dataview
TABLE WITHOUT ID
  dateformat(date, "kkkk-'W'WW") AS "Week",
  length(rows) AS "Meetings",
  sum(rows.duration_minutes) AS "Total minutes"
FROM "Meetings"
WHERE type = "meeting" AND date >= date(today) - dur(60 days)
GROUP BY dateformat(date, "kkkk-'W'WW")
SORT Week DESC
```
