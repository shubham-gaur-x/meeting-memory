# meeting-memory

Turns every meeting-related email in your Gmail into a structured Markdown note in an [Obsidian](https://obsidian.md) vault, with Dataview dashboards for People, Topics, Decisions, Action Items, and a Weekly Digest.

```
Gmail ──Composio OAuth──▶ ingest.py ──LLM extract──▶ vault/Meetings/*.md
                                                  └──▶ vault/People/*.md
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
scripts/
  backfill.py          Monthly-sliced deep backfill with progress bar
  listen.py            Long-running daemon with adaptive backoff + signal handling
  install_launchd.sh   Register listen.py as a macOS launchd service
  uninstall_launchd.sh Remove the launchd service
```

## Real-time webhook (optional)

For sub-minute latency, see `docs/webhook_setup.md`.

---

## Privacy

With `LLM_BACKEND=ollama`, email content never leaves your machine. No cloud database, no external dashboard.
