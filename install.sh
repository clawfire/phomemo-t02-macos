#!/usr/bin/env bash
# Phomemo T02 → macOS printer. Idempotent: re-run it to upgrade or repair.
#
#   ./install.sh               install / update everything
#   ./install.sh --testpage    … and print a test page through CUPS at the end
#
# What it sets up (all user-level, except the CUPS queue which needs sudo):
#   ~/Library/Application Support/PhomemoT02/{venv,app,spool,failed,config.json}
#   ~/Applications/Phomemo T02 Bridge.app      (launcher holding the Bluetooth permission)
#   ~/Library/LaunchAgents/net.clawfire.t02bridge.plist
#   ~/Library/Logs/PhomemoT02/bridge.log
#   CUPS queue "Phomemo_T02"  →  socket://127.0.0.1:9100
set -euo pipefail

LABEL="net.clawfire.t02bridge"
QUEUE="Phomemo_T02"
SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/Library/Application Support/PhomemoT02"
LOG_DIR="$HOME/Library/Logs/PhomemoT02"
APP="$HOME/Applications/Phomemo T02 Bridge.app"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=9100
TESTPAGE=0
[[ "${1:-}" == "--testpage" ]] && TESTPAGE=1

say()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "macOS uniquement."

# ── 1. Prérequis ───────────────────────────────────────────────────────────
say "Vérification des prérequis"
PY=""
for cand in "${T02_PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
  [[ -z "$cand" ]] && continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
[[ -n "$PY" ]] || die "Python ≥ 3.10 introuvable (brew install python, ou pyenv). Le python3 d'Apple (3.9) est trop vieux."
command -v clang >/dev/null 2>&1 || die "clang manquant : lance « xcode-select --install » puis relance ce script."
ok "Python : $PY ($("$PY" -V 2>&1))"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | grep -qv -e "Python" -e "COMMAND"; then
  die "Le port $PORT est déjà pris par autre chose (lsof -iTCP:$PORT). Change « port » dans config.json."
fi

# ── 2. Environnement Python + code ─────────────────────────────────────────
say "Environnement Python (venv + bleak + Pillow)"
mkdir -p "$HOME_DIR" "$LOG_DIR"
if [[ ! -x "$HOME_DIR/venv/bin/python3" ]] || ! "$HOME_DIR/venv/bin/python3" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then
  rm -rf "$HOME_DIR/venv"
  "$PY" -m venv "$HOME_DIR/venv"
fi
"$HOME_DIR/venv/bin/python3" -m pip install --quiet --upgrade pip
"$HOME_DIR/venv/bin/python3" -m pip install --quiet -r "$SRC/requirements.txt"
rm -rf "$HOME_DIR/app" && mkdir -p "$HOME_DIR/app"
cp -R "$SRC/t02bridge" "$HOME_DIR/app/"
ok "Code installé dans $HOME_DIR/app"

# ── 3. App launcher (porte la permission Bluetooth) ────────────────────────
# macOS rattache l'autorisation Bluetooth à la signature du binaire : on ne
# recompile (et ne re-signe) que si le launcher a réellement changé, sinon
# chaque mise à jour redemanderait l'accès Bluetooth.
STAMP="$HOME_DIR/launcher.sha256"
WANT="$(cat "$SRC/launcher/launcher.c" "$SRC/launcher/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
HAVE="$(cat "$STAMP" 2>/dev/null || true)"
if [[ "$WANT" != "$HAVE" || ! -x "$APP/Contents/MacOS/T02Bridge" ]] \
   || ! codesign --verify "$APP" >/dev/null 2>&1; then
  say "Compilation de « Phomemo T02 Bridge.app »"
  mkdir -p "$APP/Contents/MacOS"
  cp "$SRC/launcher/Info.plist" "$APP/Contents/Info.plist"
  clang -O2 -Wall -o "$APP/Contents/MacOS/T02Bridge" "$SRC/launcher/launcher.c" \
        -sectcreate __TEXT __info_plist "$SRC/launcher/Info.plist"
  codesign --force --sign - --identifier "$LABEL" "$APP" >/dev/null 2>&1 || true
  echo "$WANT" > "$STAMP"
  NEED_BT_PROMPT=1
  ok "App prête : $APP"
else
  NEED_BT_PROMPT=0
  ok "Launcher inchangé — signature et autorisation Bluetooth conservées"
fi

# ── 4. Stop de l'ancien agent (s'il tourne) ────────────────────────────────
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

# ── 5. Autorisation Bluetooth + détection de la T02 ────────────────────────
if (( NEED_BT_PROMPT )) || ! grep -q '"address": "' "$HOME_DIR/config.json" 2>/dev/null; then
  say "Autorisation Bluetooth — allume la T02 maintenant"
  (( NEED_BT_PROMPT )) && echo "   macOS va demander l'accès Bluetooth pour « Phomemo T02 Bridge » → clique « Autoriser »."
  SCAN_OUT="$(mktemp)"
  open -W -n "$APP" --stdout "$SCAN_OUT" --stderr "$SCAN_OUT" --args \
    /usr/bin/env "PYTHONPATH=$HOME_DIR/app" "$HOME_DIR/venv/bin/python3" -m t02bridge scan --save || true
  sed 's/^/   /' "$SCAN_OUT"; rm -f "$SCAN_OUT"
  grep -q '"address": "' "$HOME_DIR/config.json" 2>/dev/null \
    && ok "T02 mémorisée (config.json)" \
    || echo "   (pas trouvée pour l'instant — pas grave, elle sera cherchée par son nom à chaque impression)"
else
  ok "T02 déjà connue — pas de nouveau scan"
fi

# ── 6. LaunchAgent (le pont tourne en tâche de fond, relancé si besoin) ────
say "Démarrage du pont en tâche de fond"
mkdir -p "$(dirname "$AGENT")"
sed -e "s#@APP@#$APP#g" -e "s#@HOME_DIR@#$HOME_DIR#g" -e "s#@LOG_DIR@#$LOG_DIR#g" \
    "$SRC/launcher/$LABEL.plist.in" > "$AGENT"
plutil -lint "$AGENT" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$AGENT"
for _ in {1..20}; do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 0.5; done
nc -z 127.0.0.1 "$PORT" 2>/dev/null || die "Le pont n'écoute pas sur $PORT — regarde $LOG_DIR/bridge.log"
ok "Pont actif sur 127.0.0.1:$PORT"

# ── 7. File d'impression CUPS ──────────────────────────────────────────────
say "Ajout de l'imprimante « Phomemo T02 » (mot de passe admin demandé)"
sudo lpadmin -p "$QUEUE" -E \
  -v "socket://127.0.0.1:$PORT" \
  -P "$SRC/ppd/Phomemo-T02.ppd" \
  -D "Phomemo T02" -L "Bluetooth (via T02 Bridge)" \
  -o printer-is-shared=false
ok "Imprimante ajoutée : elle apparaît dans Réglages > Imprimantes et dans ⌘P"

# ── 8. Icône de barre de menus (optionnelle : l'impression marche sans) ────
MENU_APP="$HOME/Applications/Phomemo T02.app"
MENU_LABEL="net.clawfire.t02menu"
MENU_AGENT="$HOME/Library/LaunchAgents/$MENU_LABEL.plist"
say "Icône de barre de menus"
launchctl bootout "gui/$(id -u)/$MENU_LABEL" >/dev/null 2>&1 || true
if command -v swiftc >/dev/null 2>&1; then
  mkdir -p "$MENU_APP/Contents/MacOS"
  cp "$SRC/menubar/Info.plist" "$MENU_APP/Contents/Info.plist"
  if swiftc -O -o "$MENU_APP/Contents/MacOS/T02Menu" "$SRC/menubar/main.swift" \
       -framework AppKit -framework Network 2> "$LOG_DIR/menubar-build.log"; then
    codesign --force --sign - --identifier "$MENU_LABEL" "$MENU_APP" >/dev/null 2>&1 || true
    sed -e "s#@MENU_APP@#$MENU_APP#g" "$SRC/menubar/$MENU_LABEL.plist.in" > "$MENU_AGENT"
    plutil -lint "$MENU_AGENT" >/dev/null
    launchctl bootstrap "gui/$(id -u)" "$MENU_AGENT"
    ok "Icône 🖨 active dans la barre de menus (se relance à chaque session)"
  else
    echo "   ⚠︎ compilation impossible — détails : $LOG_DIR/menubar-build.log (l'impression n'est pas affectée)"
  fi
else
  echo "   ⚠︎ swiftc absent (xcode-select --install) — icône ignorée, l'impression n'est pas affectée"
fi

if (( TESTPAGE )); then
  say "Page de test via CUPS"
  TMP_PNG="$(mktemp -t t02test).png"
  PYTHONPATH="$HOME_DIR/app" "$HOME_DIR/venv/bin/python3" -c \
    "import sys; from t02bridge.testpage import make_testpage; open(sys.argv[1],'wb').write(make_testpage())" "$TMP_PNG"
  lp -d "$QUEUE" -o media=Receipt48 "$TMP_PNG" >/dev/null && ok "Envoyée — suis le log : tail -f \"$LOG_DIR/bridge.log\""
fi

echo
ok "Terminé. Imprime depuis n'importe quelle app : ⌘P → Phomemo T02."
