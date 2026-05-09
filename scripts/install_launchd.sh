#!/usr/bin/env bash
# Install the launchd agent that keeps scripts/listen.py running.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.meeting-memory"
TEMPLATE="$PROJECT/scripts/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -d "$PROJECT/.venv" ]]; then
  echo "✗ .venv missing. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$PROJECT/logs"
mkdir -p "$HOME/Library/LaunchAgents"

# Materialize the plist with absolute paths.
sed "s|__PROJECT__|$PROJECT|g" "$TEMPLATE" > "$TARGET"

# Reload if already loaded.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"
launchctl enable   "gui/$UID/$LABEL"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "✓ installed: $TARGET"
echo "  status:    launchctl print gui/$UID/$LABEL | head -20"
echo "  logs:      tail -f $PROJECT/logs/listen.out.log"
echo "  stop:      bash scripts/uninstall_launchd.sh"
