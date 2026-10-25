#!/usr/bin/env bash
# Remove the T02 printer. Keeps config + failed jobs unless --purge.
set -uo pipefail

LABEL="net.clawfire.t02bridge"
HOME_DIR="$HOME/Library/Application Support/PhomemoT02"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "✓ pont arrêté"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
lpstat -p Phomemo_T02 >/dev/null 2>&1 && sudo lpadmin -x Phomemo_T02 && echo "✓ imprimante supprimée"
rm -rf "$HOME/Applications/Phomemo T02 Bridge.app" "$HOME_DIR/venv" "$HOME_DIR/app"
tccutil reset Bluetooth "$LABEL" >/dev/null 2>&1 || true

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$HOME_DIR" "$HOME/Library/Logs/PhomemoT02"
  echo "✓ config, spool et logs effacés"
else
  echo "  (config et jobs en échec conservés dans $HOME_DIR — --purge pour tout effacer)"
fi
