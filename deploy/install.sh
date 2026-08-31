#!/usr/bin/env bash
# Install Autonomous as a background service that starts when you log in.
#
#   ./deploy/install.sh            install and start
#   ./deploy/install.sh --uninstall  stop and remove
#
# Linux uses a systemd *user* unit (no root). macOS uses a launchd agent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
LABEL="dev.autonomous.panel"
UNINSTALL=false
[[ "${1:-}" == "--uninstall" ]] && UNINSTALL=true

if [[ ! -x "$PYTHON" ]]; then
  echo "error: no virtualenv at $REPO/.venv" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

case "$(uname -s)" in
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT="$UNIT_DIR/autonomous.service"

    if $UNINSTALL; then
      systemctl --user disable --now autonomous.service 2>/dev/null || true
      rm -f "$UNIT"
      systemctl --user daemon-reload
      echo "Removed."
      exit 0
    fi

    mkdir -p "$UNIT_DIR"
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Autonomous panel
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO
ExecStart=$PYTHON -m autonomous.cli serve
Restart=always
RestartSec=10
# Reads .env from WorkingDirectory; no secrets in this unit file.

[Install]
WantedBy=default.target
UNIT_EOF

    systemctl --user daemon-reload
    systemctl --user enable --now autonomous.service
    echo "Installed and started."
    echo
    echo "  status:  systemctl --user status autonomous"
    echo "  logs:    journalctl --user -u autonomous -f"
    echo "  stop:    systemctl --user stop autonomous"
    echo
    echo "To keep it running while you are logged out:"
    echo "  sudo loginctl enable-linger ${USER:-$(id -un)}"
    ;;

  Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    LOG_DIR="$REPO/data"

    if $UNINSTALL; then
      launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"
      echo "Removed."
      exit 0
    fi

    mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>autonomous.cli</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/panel.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/panel.log</string>
</dict>
</plist>
PLIST_EOF

    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Installed and started."
    echo
    echo "  logs:    tail -f $LOG_DIR/panel.log"
    echo "  stop:    launchctl unload $PLIST"
    echo
    echo "Note: macOS suspends agents while the machine sleeps."
    echo "To keep watchers polling with the lid open, see: caffeinate -s"
    ;;

  *)
    echo "error: unsupported platform $(uname -s)." >&2
    echo "Run it manually with: $PYTHON -m autonomous.cli serve" >&2
    exit 1
    ;;
esac

PORT="$(grep -E '^PORT=' "$REPO/.env" 2>/dev/null | cut -d= -f2 || true)"
echo
echo "Panel: http://127.0.0.1:${PORT:-8000}"
