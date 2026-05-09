---
type: dashboard
---

# People

## Most-met (last 90 days)

```dataview
TABLE WITHOUT ID
  attendee.name AS "Person",
  length(rows) AS "Meetings",
  max(rows.date) AS "Last",
  min(rows.date) AS "First"
FROM "Meetings"
FLATTEN attendees AS attendee
WHERE type = "meeting" AND date >= date(today) - dur(90 days) AND attendee.name
GROUP BY attendee.name
SORT length(rows) DESC
LIMIT 25
```

## Open action items per person

```dataview
TABLE WITHOUT ID
  item.owner AS "Owner",
  length(rows) AS "Open",
  min(rows.item.due) AS "Earliest due"
FROM "Meetings"
FLATTEN action_items AS item
WHERE type = "meeting" AND item.done != true AND item.owner
GROUP BY item.owner
SORT length(rows) DESC
```

## Person notes

```dataview
LIST
FROM "People"
WHERE type = "person"
SORT file.name ASC
```
