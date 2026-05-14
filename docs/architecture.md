# Meeting Memory — Architecture & Implementation Walkthrough

## What It Solves

Every meeting generates follow-up work — action items, decisions, and notes — that gets buried in email threads. This system automatically reads your Gmail, identifies meeting-related emails, uses an AI model to extract structured information, writes organized notes into an Obsidian knowledge base, and creates Jira tickets for all action items. Zero manual entry.

---

## High-Level Pipeline

```
Gmail
  │
  ▼ (Composio OAuth REST API)
ComposioGmail — fetches raw email payloads
  │
  ▼
Classifier — rules-based scoring, no AI cost
  │
  ├─ score < 0.6 → skip
  │
  ▼ score ≥ 0.6
Extractor — LLM call (Claude or Ollama)
  │
  ├─ confidence < 0.3 → skip
  │
  ├──▶ Obsidian Vault (vault/Meetings/*.md + vault/People/*.md)
  │
  └──▶ Jira Cloud (if JIRA_ENABLED=true)
              ├─ high priority → active sprint
              └─ medium/low → backlog
```

Every step is **idempotent** — Gmail message IDs are tracked in `state.json`, so re-running the pipeline never creates duplicate notes or Jira tickets.

---

## Component Deep Dive

### 1. Gmail Ingestion — `lib/composio_gmail.py`

Rather than giving the system direct Gmail API credentials, it uses **Composio** as an OAuth broker. Composio holds the user's Gmail OAuth tokens; the pipeline calls Composio's REST API (v3.1), which proxies to Gmail.

This was done intentionally — Composio's Python SDK had a `pydantic` schema bug that crashed on import, so the client was written as a direct `httpx` REST caller with no SDK dependency.

Composio can return emails in two different wire shapes (raw Gmail API format vs. flattened Composio format). Both are normalized into a single `GmailMessage` dataclass with consistent fields: `message_id`, `sender_email`, `sender_name`, `subject`, `body_text`, `received_at`, `to`, `cc`, `labels`.

---

### 2. Classification — `lib/classifier.py`

Before paying for any AI call, every email is scored by a **rules engine** with no LLM involved. This keeps costs near zero for the majority of emails that aren't meeting-related.

The scorer checks four signals:

| Signal | Score boost | Example |
|---|---|---|
| Known sender address | +0.8 | `no-reply@zoom.us`, `fathom@fathom.video`, `gemini-notes@google.com` |
| Known sender domain | +0.5 | `otter.ai`, `fireflies.ai`, `calendly.com` |
| Subject keyword match | +0.4 | "sync", "standup", "1:1", "recap", "action items" |
| Body keyword match | +0.3 | "join the meeting", `zoom.us/j/`, "attendees:", "agenda" |
| iCalendar attachment | +0.5 | `BEGIN:VCALENDAR` in the body |

A total score ≥ 0.6 → treated as a meeting. The classifier also assigns a `kind`: `invite`, `recording`, `transcript`, `recap`, or `conversation`.

---

### 3. AI Extraction — `lib/extractor.py`

Meeting emails that pass classification are sent to an LLM with a strict JSON schema prompt. The model returns a structured object — no free text.

**What gets extracted:**

```
title, kind, platform, date, start_time, end_time, duration_minutes,
attendees (name, email, role), summary, topics, decisions,
action_items (owner, task, due, priority), key_quotes, links,
sentiment, follow_up_needed, confidence
```

The **priority field** on each action item (`high` / `medium` / `low`) was specifically added for the Jira integration. The LLM prompt defines it precisely:
- **High** = blocking, urgent, "must be done this week", "ASAP", "before next meeting"
- **Medium** = important with a clear deadline within the month
- **Low** = nice-to-have, far future, or no clear deadline

**Two LLM backends are supported**, selected via `LLM_BACKEND` in `.env`:

- **Anthropic Claude** (`claude-sonnet-4-6`) — cloud, highest quality, prompt caching enabled to reduce token costs on repeated system prompts
- **Ollama** (`qwen2.5:7b` or similar) — fully local, email content never leaves the machine, zero API cost

Both backends go through `_coerce_json_object()` — a parser that handles models occasionally wrapping JSON in markdown fences or adding extra prose. It walks the character stream to find the first balanced `{...}` block.

Emails where the model returns `confidence < 0.3` are silently skipped — the system self-reports when it isn't sure.

---

### 4. Obsidian Notes — `lib/obsidian_writer.py`

Each meeting that passes extraction gets written to `vault/Meetings/YYYY-MM-DD - <title>.md`.

The file has two parts:

**YAML frontmatter** — machine-readable, consumed by Obsidian's Dataview plugin for dashboard queries:

```yaml
---
type: meeting
date: 2026-05-06
platform: google-meet
attendees: [{name: Alice Patel, email: alice@acme.com, role: attendee}]
topics: [pricing, q3-roadmap]
decisions: ["Move launch to July 15"]
action_items:
  - {owner: Alice, task: "Send revised SOW", due: 2026-05-09, done: false, priority: high}
source: {message_id: 18f9ac…, subject: "Recap: …"}
---
```

**Markdown body** — human-readable with sections for Summary, Attendees, Decisions, Action Items, Topics, Key Quotes, and the original email body collapsed inside a `<details>` tag.

The writer also maintains **per-person notes**. For every attendee, it creates `vault/People/<name>.md` on first encounter with a backlink to the meeting, then appends new meeting backlinks on subsequent meetings with that person. This builds a contact history automatically.

---

### 5. State Tracking — `lib/state.py`

A thin JSON store (`state.json`) that maps Gmail message IDs to their processed state. It serves as the deduplication layer — `state.has(message_id)` is checked before every processing step.

After the Jira integration was added, the state schema was extended to track Jira push status per message:

```json
{
  "18f9ac123": {
    "note": "/vault/Meetings/2026-05-06 - Pricing Review.md",
    "kind": "recap",
    "confidence": 0.95,
    "jira_issues": [
      {
        "key": "SCRUM-42",
        "url": "https://…/browse/SCRUM-42",
        "kind": "action_item",
        "summary": "Send revised SOW",
        "priority": "high",
        "pushed_at": "2026-05-08T17:00:00+00:00"
      }
    ],
    "jira_pushed_at": "2026-05-08T17:00:00+00:00"
  }
}
```

All writes go through a `threading.Lock` and are atomic via a `.tmp` rename — no partial writes if the process crashes mid-run.

---

### 6. Jira Integration — `lib/jira_client.py` + `lib/jira_pusher.py`

This was added as a complete feature on top of the existing pipeline.

#### `lib/jira_client.py` — REST Client

Uses **HTTP Basic auth** with base64-encoded `email:api_token` — exactly how Jira Cloud expects it, no SDK dependency.

Implements two API surfaces with a single client class:

**Jira REST API v3** (`/rest/api/3/`) — via a persistent `httpx.Client` with `base_url` set:
- `get_myself()` — credential validation at startup
- `get_project(key)` — project existence check
- `create_issue(fields)` — creates the Jira ticket
- `search_users(query)` — resolve owner names to Jira account IDs
- `add_comment()`, `delete_issue()` — utility operations

**Jira Agile API** (`/rest/agile/1.0/`) — sprint management, called with full URLs using module-level `httpx` (separate base path, cannot share the same client):
- `get_active_sprint(board_id)` — finds the currently active sprint
- `add_to_sprint(sprint_id, issue_keys)` — moves issues into sprint in one batch call

All calls are wrapped with `with_retry()` from `lib/utils.py`, which provides exponential backoff for transient 5xx errors. Non-retryable 4xx errors (bad credentials, missing project) raise `JiraError` immediately.

The client implements `__enter__`/`__exit__` for use as a context manager, ensuring the `httpx.Client` connection pool is properly closed.

#### `lib/jira_pusher.py` — Push Logic

This is where the meeting data becomes Jira tickets.

**Priority routing** is the core business logic:

```
Action item has "priority" field?
  ├─ yes → use it directly (set by LLM)
  └─ no (legacy notes without priority field) → due-date heuristic:
       ├─ due ≤ 14 days from today → high
       ├─ due ≤ 60 days → medium
       └─ else or no due date → low

high priority → create issue → batch-add to active sprint
medium/low → create issue → stays in backlog
```

Each Jira issue is created with:
- **Summary** — the action item task text
- **Priority** — mapped to Jira's High/Medium/Low field
- **Due date** — if extracted (field omitted entirely when null — Jira rejects explicit null for date fields)
- **Labels** — `meeting-generated` + all topic tags from the meeting
- **Assignee** — resolved from `JIRA_ASSIGNEE_MAP` (JSON map of owner names → Jira account IDs), with first-name fallback matching and optional email-based API lookup
- **Description** — formatted using Atlassian Document Format (ADF), Jira's native rich-text format (a nested dict structure, built without any external library)

The ADF description contains: meeting title and date, the specific action item, meeting summary, decisions, attendee list, and a deep link back to the Obsidian note via an `obsidian://` URI.

**Sprint assignment** happens in one batch call at the end — all high-priority keys collected during the loop are sent to `add_to_sprint()` together, minimizing API calls.

---

### 7. Standalone Backfill — `scripts/push_to_jira.py`

Lets you push historical vault notes to Jira without re-running the Gmail pipeline. It reads `state.json` to find which notes haven't been pushed, reconstructs the `Extracted` dataclass from the YAML frontmatter of each vault note, and calls the same `push_meeting()` function.

CLI options:

```bash
python scripts/push_to_jira.py                     # all unpushed
python scripts/push_to_jira.py --dry-run            # preview only
python scripts/push_to_jira.py --message-id <id>   # one specific note
python scripts/push_to_jira.py --since 2026-05-01  # filter by date
python scripts/push_to_jira.py --force             # re-push already-pushed
python scripts/push_to_jira.py --create-decisions  # also push decisions
```

After each successful push, it appends a `## Jira Issues` table to the vault note with clickable links to every created ticket. This append is **idempotent** — if the section already exists, it is skipped.

---

### 8. Setup Wizard — `setup.py`

An interactive terminal wizard that walks through the entire configuration in one session:

1. Python environment setup
2. Composio API key validation
3. LLM backend selection (Claude vs Ollama)
4. Credential validation for the chosen backend
5. Gmail OAuth via Composio
6. **Jira configuration** (optional, skippable):
   - Collects domain, email, API token, project key, board ID
   - Validates credentials via `GET /rest/api/3/myself`
   - Validates project key via `GET /rest/api/3/project/{key}`
   - Auto-enables sprint routing if a board ID is provided
7. Writes `.env` with all collected values

---

### 9. Operations

**Running modes:**

```bash
make run          # one-off, processes last hour's emails
make backfill     # deep historical backfill (last 12 months)
make start        # installs a macOS launchd daemon — runs every minute
make jira-dry     # preview what would be pushed to Jira (no API calls)
make jira         # push all unpushed vault notes to Jira
```

**Resilience design choices:**
- Jira push errors in `ingest.py` are **caught and logged, never fatal** — a Jira API outage does not corrupt vault notes or lose state
- State is saved **per-note** in `push_to_jira.py`, not batched — a mid-backfill crash loses at most one note's push record
- All HTTP calls use exponential backoff via `with_retry()` before surfacing as errors

---

## Data Flow Summary

```
1. Email arrives in Gmail
2. Composio OAuth proxy → normalized GmailMessage
3. Rules classifier scores the email (no LLM, ~1ms)
   → score < 0.6: discard
4. LLM (Claude/Ollama) extracts structured JSON from the email body
   → confidence < 0.3: discard
5. Obsidian writer creates vault/Meetings/YYYY-MM-DD - Title.md
   → YAML frontmatter (queryable by Dataview)
   → Markdown body (human-readable)
   → vault/People/Name.md (upserted with meeting backlink)
6. State saved: message_id → note_path (deduplication)
7. [If JIRA_ENABLED=true] For each action_item:
   a. Priority determined (LLM field or due-date heuristic)
   b. Jira issue created via REST API
   c. High-priority issues batch-moved to active sprint
   d. State updated with jira_issues list
   e. ## Jira Issues table appended to vault note
```

---

## Key Design Decisions

**No external SDK for Jira or Composio** — both are direct `httpx` REST calls. This avoids version lock-in and the specific Composio SDK crash that triggered the approach.

**Two-phase filtering (rules then LLM)** — the classifier eliminates ~90% of emails before any LLM call, keeping API costs minimal.

**Priority lives in the LLM prompt** — rather than post-processing, the model assigns priority as part of structured extraction. The due-date heuristic is only a fallback for pre-existing notes that predate the feature.

**Sprint routing is a single batch call** — rather than calling `add_to_sprint` once per high-priority issue, all keys are collected and added in one API call at the end of each meeting's push.

**Vault notes are the source of truth for backfill** — `push_to_jira.py` reconstructs `Extracted` from YAML frontmatter, so Jira pushes work even without re-fetching the original email.
