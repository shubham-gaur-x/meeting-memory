#!/usr/bin/env bash
set -euo pipefail
LABEL="com.meeting-memory"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$TARGET"
echo "✓ uninstalled $LABEL"
