# meeting-memory

Turns every meeting-related email in your Gmail into a structured Markdown note in an [Obsidian](https://obsidian.md) vault — and optionally creates Jira stories from action items automatically.

```
Gmail ──Composio OAuth──▶ ingest.py ──LLM extract──▶ vault/Meetings/*.md
                                                  └──▶ vault/People/*.md
                                                  └──▶ Jira (if enabled)
                                                            │
                                               Dataview dashboards (Obsidian)
```

No subscriptions required. Uses Composio's free tier for Gmail OAuth and either the Anthropic API or a local Ollama model for extraction.

---

## Quickstart

**Requirements:** Python 3.9+, macOS (for the background daemon; the pipeline itself is cross-platform)

```bash
git clone <repo-url>
cd meeting-memory
make setup      # wizard: creates venv, validates credentials, connects Gmail
make backfill   # catch up on the last 12 months
make start      # install background daemon — stays running, polls every minute
```

Open `vault/` as an Obsidian vault, enable the **Dataview** community plugin, and start at `vault/_Dashboards/00 - Home.md`.

---

## Daily use

```bash
make run        # one-off ingest (last hour)
make backfill   # deep historical backfill
make status     # check daemon health
make logs       # tail live daemon output
make stop       # stop the daemon
make reset      # wipe state — re-processes everything on next run
```

---

## Jira integration

Automatically creates Jira stories from meeting action items. The LLM assigns each action item a priority (`high`, `medium`, or `low`). **High-priority issues go directly into the active sprint; medium and low stay in the backlog.**

### Setup

Run the setup wizard and fill in Step 6:

```bash
python setup.py --force
```

Or add these to your `.env` manually:

```bash
JIRA_ENABLED=true
JIRA_DOMAIN=yourcompany.atlassian.net   # no https://
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=                          # id.atlassian.com → Security → API tokens
JIRA_PROJECT_KEY=PROJ                    # e.g. PROJ, ENG, SCRUM
JIRA_BOARD_ID=1                          # numeric ID from board URL …/boards/1
JIRA_ISSUE_TYPE=Task                     # Task | Story
```

### How priority routing works

The LLM extracts a `priority` field for every action item based on context:

| Priority | When | Jira destination |
|----------|------|-----------------|
| `high` | Blocking, urgent, due this week, or "ASAP" | **Active sprint** |
| `medium` | Important, deadline within the month | Backlog |
| `low` | Nice-to-have, far future, or no deadline | Backlog |

For notes processed before the priority field was added, a due-date heuristic applies: due within 14 days → high, within 60 days → medium, else low.

### What each Jira issue contains

Each issue is created with:
- **Summary** — the action item task text
- **Priority** — mapped to Jira's High / Medium / Low field
- **Due date** — if extracted from the meeting email
- **Labels** — `meeting-generated` + topic tags from the meeting
- **Description** — meeting title, date, summary, decisions made, attendees, and a deep link back to the Obsidian note

### Backfill and manual push

```bash
make jira-dry                                  # preview what would be pushed
make jira                                      # push all unpushed notes
python scripts/push_to_jira.py --since 2026-05-01   # filter by date
python scripts/push_to_jira.py --message-id <id>    # push one specific note
python scripts/push_to_jira.py --force              # re-push already-pushed notes
python scripts/push_to_jira.py --create-decisions   # also create issues for decisions
```

After a successful push, a `## Jira Issues` table is appended to the Obsidian note with clickable links to every created issue.

### Assignee mapping

To auto-assign issues, add a JSON map of owner names → Jira account IDs to `.env`:

```bash
JIRA_ASSIGNEE_MAP={"alice smith": "5f3d...", "bob jones": "6a1c..."}
```

Get account IDs from: `https://yourcompany.atlassian.net/rest/api/3/user/search?query=name`

---

## What you see in Obsidian

Every meeting note has YAML frontmatter so Dataview can slice and dice it:

```yaml
---
type: meeting
date: 2026-05-06
platform: google-meet
attendees: [{name: Alice Patel, email: alice@acme.com, role: attendee}]
topics: [pricing, q3-roadmap]
decisions: ["Move launch to July 15"]
action_items:
  - {owner: Alice, task: "Send revised SOW", due: 2026-05-09, done: false}
source: {message_id: 18f9ac…, subject: "Recap: …"}
---
```

Dashboards at `vault/_Dashboards/`:
- **People** — who you meet most, last contact, open action items
- **Topics** — auto-extracted topics, frequency over time
- **Decisions log** — every decision linked back to its meeting
- **Action items** — open/done, owner, due date
- **Weekly digest** — rolling 7-day summary
- **Calendar** — meetings grouped by day

---

## Switching LLM backend

Edit `LLM_BACKEND` in `.env`:

| Backend | Value | Notes |
|---------|-------|-------|
| Anthropic Claude | `anthropic` | Best quality; requires `ANTHROPIC_API_KEY` |
| Ollama (local) | `ollama` | Fully private; run `ollama serve && ollama pull qwen2.5:7b` |

---

## Architecture

```
ingest.py              entry point — CLI args, orchestration
lib/
  composio_gmail.py    Composio v3.1 REST client (no SDK), Gmail fetch + normalize
  classifier.py        Rules-based meeting scorer (no LLM)
  extractor.py         LLM extraction → structured Extracted dataclass
  obsidian_writer.py   Markdown + YAML frontmatter writer
  state.py             Processed-ID tracker (state.json)
  utils.py             with_retry() — exponential backoff for all HTTP calls
  jira_client.py       Jira Cloud REST API v3 client (Basic auth, retry, Agile API)
  jira_pusher.py       Push logic, priority routing, ADF description builder
scripts/
  backfill.py          Monthly-sliced deep backfill with progress bar
  listen.py            Long-running daemon with adaptive backoff + signal handling
  push_to_jira.py      Standalone Jira backfill CLI
  install_launchd.sh   Register listen.py as a macOS launchd service
  uninstall_launchd.sh Remove the launchd service
```

## Real-time webhook (optional)

For sub-minute latency, see `docs/webhook_setup.md`.

---

## Privacy

With `LLM_BACKEND=ollama`, email content never leaves your machine. No cloud database, no external dashboard.
