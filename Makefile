# meeting-memory — Gmail → Obsidian meeting pipeline
# Run `make` or `make help` to see all commands.

SHELL   := /bin/bash
PYTHON  := .venv/bin/python
PIP     := .venv/bin/pip
LABEL   := com.meeting-memory

.DEFAULT_GOAL := help

.PHONY: help install setup run backfill start stop status logs reset jira jira-dry memgraph memgraph-dry syncthing-status

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?##"}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

_venv_guard:
	@test -f $(PYTHON) || { echo "  Run 'make install' first"; exit 1; }

install: ## Create .venv and install Python dependencies
	python3 -m venv .venv
	$(PIP) install -q -r requirements.txt
	@echo "  ✓ Dependencies installed. Run 'make setup' next."

setup: ## First-run wizard: credentials, Gmail OAuth, write .env
	@test -f $(PYTHON) || $(MAKE) --no-print-directory install
	$(PYTHON) setup.py

run: _venv_guard ## One-off ingest for the last hour
	$(PYTHON) ingest.py --since "1 hour ago"

backfill: _venv_guard ## Deep historical backfill (default: 12 months)
	$(PYTHON) scripts/backfill.py

start: ## Install and start background daemon (macOS launchd)
	bash scripts/install_launchd.sh

stop: ## Stop and uninstall background daemon
	bash scripts/uninstall_launchd.sh

status: ## Show daemon health and last 20 log lines
	@launchctl print "gui/$$(id -u)/$(LABEL)" 2>/dev/null \
		|| echo "  Daemon not running (use 'make start')"
	@echo ""
	@echo "--- Recent output ---"
	@tail -20 logs/listen.out.log 2>/dev/null || echo "  (no logs yet)"

logs: ## Tail live daemon logs (Ctrl-C to exit)
	tail -f logs/listen.out.log

reset: _venv_guard ## Wipe state.json so all emails are re-processed on next run
	$(PYTHON) -c "import json; open('state.json','w').write(json.dumps({'processed':{},'last_run':None}, indent=2))"
	@echo "  ✓ state.json reset"

jira: _venv_guard ## Push unpushed meeting notes to Jira
	$(PYTHON) scripts/push_to_jira.py

jira-dry: _venv_guard ## Preview what would be pushed to Jira (no API calls)
	$(PYTHON) scripts/push_to_jira.py --dry-run

memgraph: _venv_guard ## Push all vault notes to Memgraph graph database
	$(PYTHON) scripts/push_to_memgraph.py

memgraph-dry: _venv_guard ## Preview what push_to_memgraph.py would write (no DB calls)
	$(PYTHON) scripts/push_to_memgraph.py --dry-run

syncthing-status: ## Show Syncthing vault folder sync status
	@curl -sf \
	  -H "X-API-Key: $$(grep '^SYNCTHING_API_KEY' .env | cut -d= -f2)" \
	  "http://127.0.0.1:8384/rest/db/status?folder=$$(grep '^SYNCTHING_FOLDER_ID' .env | cut -d= -f2)" \
	  | python3 -m json.tool || echo "  Syncthing not reachable (is it running?)"
