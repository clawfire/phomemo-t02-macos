# Phomemo T02 → imprimante macOS

Ta T02 apparaît dans **Réglages › Imprimantes** et dans **⌘P** comme n'importe quelle imprimante. La conversion et l'envoi Bluetooth se font **en tâche de fond** : la file macOS se vide instantanément, le pont s'occupe du reste (et réessaie si la T02 dort).

```
App (⌘P) ─► macOS rend la page ─► CUPS ─► socket://127.0.0.1:9100
                                              │
                         Phomemo T02 Bridge (LaunchAgent, Python)
                         spool ► tramage 384 px ► ESC/POS T02 ► BLE ─► 🖨
```

## Installation

```bash
brew install python          # Python ≥ 3.10 (ou pyenv) — le python3 d'Apple est trop vieux
xcode-select --install       # si clang manque
./install.sh --testpage      # allume la T02 avant
```

Pendant l'install, macOS demande l'accès Bluetooth pour **« Phomemo T02 Bridge »** → *Autoriser*. Puis un mot de passe admin pour ajouter la file CUPS. C'est tout.

Re-lancer `install.sh` = mise à jour / réparation (idempotent). `./uninstall.sh [--purge]` pour tout retirer.

## À l'usage

- **⌘P → Phomemo T02**, format **« Ticket 48 mm »** par défaut : le blanc en haut/bas est coupé automatiquement.
- Formats fixes `48 × 30…200 mm` pour les étiquettes, ou taille personnalisée (largeur ≤ 48 mm).
- Options dans la feuille d'impression (*Fonctionnalités de l'imprimante*) :

| Option | Choix |
|---|---|
| Densité | Clair · **Normal** · Foncé · Très foncé |
| Tramage | **Auto** (texte net + gris tramés) · Floyd-Steinberg · Seuil · Atkinson |
| Avance papier | Aucune · +5 / +10 / +15 mm |
| Couper le blanc | **Oui** · Non (taille exacte, pour étiquettes) |

- T02 éteinte ? Le job attend dans le spool, réessais pendant ~9 min, notification macOS si besoin. Plus de papier / capot ouvert → notification.

## Commandes utiles

```bash
B="$HOME/Library/Application Support/PhomemoT02"
alias t02='PYTHONPATH="$B/app" "$B/venv/bin/python3" -m t02bridge'

t02 scan --save              # retrouver / épingler la T02
t02 print photo.jpg          # impression directe, sans CUPS
t02 testpage --density 3
tail -f ~/Library/Logs/PhomemoT02/bridge.log
ls "$B/failed"               # jobs abandonnés (re-imprimables avec t02 print)
```

Réglages par défaut (densité, tramage, port, nom BLE…) : `~/Library/Application Support/PhomemoT02/config.json`. Redémarrer le pont : `launchctl kickstart -k gui/$(id -u)/net.clawfire.t02bridge`.

> ⚠️ Pour les commandes `t02 scan/print` lancées dans le Terminal, c'est **Terminal** qui doit avoir l'accès Bluetooth (Réglages › Confidentialité › Bluetooth). Le pont, lui, utilise sa propre autorisation.

## Choix techniques (le « pourquoi »)

- **Pas de filtre ni de backend CUPS maison.** `/usr/libexec/cups` est protégé par SIP. Le PPD déclare le raster CUPS comme format final (`cupsFilter … -`) : macOS fait le rendu PDF avec son propre filtre, et le backend `socket://` intégré livre le flux au pont. Les options du PPD voyagent dans l'en-tête raster (`cupsInteger0-3`).
- **Un mini launcher C dans un `.app`.** macOS tue tout process qui touche CoreBluetooth sans `NSBluetoothAlwaysUsageDescription` dans *son* Info.plist. Le launcher porte cette clé et lance Python en processus enfant, qui hérite de l'autorisation.
- **Protocole** repris octet pour octet de l'app officielle via [TiMini-Print](https://github.com/Dejniel/TiMini-Print), recoupé avec [phomymo](https://github.com/transcriptionstream/phomymo) et [phomemo-tools](https://github.com/vivier/phomemo-tools) : GATT `ff00/ff02/ff03`, `GS v 0` par blocs de 255 lignes, paquets de 128 o / 20 ms.
- **Tramage auto hybride** : seuil net sur le texte, Floyd-Steinberg seulement sur les aplats gris (détection par filtre min/max 5×5, qui ignore l'anticrénelage des lettres).

## Si Apple coupe les drivers PPD

CUPS affiche déjà « *Printer drivers are deprecated* ». Le jour où `lpadmin -P` ne marche plus, la voie de sortie est de faire parler le pont en **IPP Everywhere** (macOS l'ajoute alors sans driver). Le cœur (raster PWG déjà supporté, tramage, BLE) ne change pas — seule la porte d'entrée change.

## Dev

```bash
python3 -m pytest -q tests/       # 17 tests, sans imprimante ni Bluetooth
python3 tools/gen_ppd.py          # régénère le PPD (ajoute tes formats dans SIZES)
python3 -m t02bridge dry-run job.ras -o out.bin   # octets bruts sans BLE
```

Testé : parsing raster v1/v2/v3 (big & little endian), octets du protocole, retry/spool, et une vraie chaîne CUPS (`lp` d'un PDF → PPD → socket → pont). Non testé ici : CoreBluetooth sur macOS 27 et la T02 physique — c'est le premier `install.sh --testpage` qui le dira.
