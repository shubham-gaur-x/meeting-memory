---
type: dashboard
---

# Action items

> Action items are stored as structured rows on each meeting note's frontmatter (`action_items:`). These dashboards flatten them.

## Open — by owner

```dataview
TABLE WITHOUT ID
  item.owner AS "Owner",
  item.task AS "Task",
  item.due AS "Due",
  file.link AS "Meeting",
  date AS "Date"
FROM "Meetings"
FLATTEN action_items AS item
WHERE type = "meeting" AND item.done != true
SORT item.due ASC, date DESC
```

## Open — assigned to me

> **Setup:** Replace `your name` on line 10 below with your first name (lowercase) so this filter matches your action items.

```dataview
TABLE WITHOUT ID
  item.task AS "Task",
  item.due AS "Due",
  file.link AS "Meeting",
  date AS "Date"
FROM "Meetings"
FLATTEN action_items AS item
WHERE type = "meeting" AND item.done != true
  AND (
    contains(lower(string(item.owner)), "your name") OR
    contains(lower(string(item.owner)), "me") OR
    contains(lower(string(item.owner)), "i ")
  )
SORT item.due ASC
```

## Overdue

```dataview
TABLE WITHOUT ID
  item.owner AS "Owner",
  item.task AS "Task",
  item.due AS "Due",
  file.link AS "Meeting"
FROM "Meetings"
FLATTEN action_items AS item
WHERE type = "meeting"
  AND item.done != true
  AND item.due
  AND date(item.due) < date(today)
SORT item.due ASC
```

## Closed in the last 14 days

```dataview
TABLE WITHOUT ID
  item.owner AS "Owner",
  item.task AS "Task",
  file.link AS "Meeting"
FROM "Meetings"
FLATTEN action_items AS item
WHERE type = "meeting" AND item.done = true AND date >= date(today) - dur(14 days)
SORT date DESC
```
