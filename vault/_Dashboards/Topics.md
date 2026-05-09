---
type: dashboard
---

# Topics

## Top topics (last 90 days)

```dataview
TABLE WITHOUT ID
  topic AS "Topic",
  length(rows) AS "Meetings",
  max(rows.date) AS "Last seen"
FROM "Meetings"
FLATTEN topics AS topic
WHERE type = "meeting" AND date >= date(today) - dur(90 days)
GROUP BY topic
SORT length(rows) DESC
LIMIT 30
```

## Recent topics

```dataview
LIST WITHOUT ID
  string(topics) + "  —  " + file.link
FROM "Meetings"
WHERE type = "meeting" AND date >= date(today) - dur(14 days)
SORT date DESC
```

## All-time tag cloud

```dataview
TABLE WITHOUT ID topic AS "Topic", length(rows) AS "n"
FROM "Meetings"
FLATTEN topics AS topic
WHERE type = "meeting"
GROUP BY topic
SORT length(rows) DESC
```
