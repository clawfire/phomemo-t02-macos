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

Re-lancer `install.sh` = mise à jour / réparation (idempotent). Le launcher n'est recompilé que s'il a changé : l'autorisation Bluetooth survit aux mises à jour. `./uninstall.sh [--purge]` pour tout retirer.

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

## Barre de menus

Une icône 🖨 (app native, sans dépendance) montre d'un coup d'œil :

- **l'état** : prête, impression, plus de papier, en attente. Le ⚠︎ n'apparaît que si un job est bloqué ou a raté ; une T02 éteinte, c'est normal ;
- **la batterie et le papier**, tels que remontés au dernier job ou à la dernière vérification ;
- **la file** : jobs en attente, jobs ratés, dernier job.

Actions : *Vérifier l'imprimante*, *Imprimer une page de test*, *Relancer les jobs ratés*, journal, réglages, redémarrer le pont.

L'app ne touche jamais au Bluetooth : elle lit `status.json` et parle au pont par son socket local. *Quitter* la ferme jusqu'à la prochaine session. Si elle n'a pas compilé, le détail est dans `~/Library/Logs/PhomemoT02/menubar-build.log` ; l'impression n'en dépend pas.

## Commandes utiles

```bash
B="$HOME/Library/Application Support/PhomemoT02"
alias t02='PYTHONPATH="$B/app" "$B/venv/bin/python3" -m t02bridge'

t02 scan --save              # retrouver / épingler la T02
t02 print photo.jpg          # impression directe, sans CUPS
t02 testpage --density 3
t02 status                   # état du pont et de la T02
t02 status --ping            # … après avoir interrogé la T02 (batterie, papier)
t02 retry --list             # jobs ratés (gardés 7 jours)
t02 retry                    # … renvoyés au pont, qui les réimprime
tail -f ~/Library/Logs/PhomemoT02/bridge.log
```

Réglages par défaut dans `~/Library/Application Support/PhomemoT02/config.json` :

| Clé | Défaut | Rôle |
|---|---|---|
| `density`, `dither`, `trim` | `2`, `"auto"`, `true` | valeurs si l'app n'en choisit pas |
| `keepalive_seconds` | `60` | connexion BLE gardée ouverte entre deux jobs (`0` = coupe à chaque fois) |
| `failed_retention_days` | `7` | âge max des jobs ratés (`0` = jamais supprimés) |
| `photo_dither` | `"atkinson"` | tramage des zones grises en mode Auto (`"floyd"` pour l'ancien rendu) |
| `printer_name`, `port` | `"T02"`, `9100` | nom Bluetooth, port local |

Après modification : `launchctl kickstart -k gui/$(id -u)/net.clawfire.t02bridge`.

**Logs** : `bridge.log` tourne tout seul (1 Mo × 3 fichiers). `launchd.log`, à côté, ne contient que d'éventuels crashs.

> ⚠️ Pour les commandes `t02 scan/print` lancées dans le Terminal, c'est **Terminal** qui doit avoir l'accès Bluetooth (Réglages › Confidentialité › Bluetooth). Le pont, lui, utilise sa propre autorisation.

## Choix techniques (le « pourquoi »)

- **Pas de filtre ni de backend CUPS maison.** `/usr/libexec/cups` est protégé par SIP. Le PPD déclare le raster CUPS comme format final (`cupsFilter … -`) : macOS fait le rendu PDF avec son propre filtre, et le backend `socket://` intégré livre le flux au pont. Les options du PPD voyagent dans l'en-tête raster (`cupsInteger0-3`).
- **Un mini launcher C dans un `.app`.** macOS tue tout process qui touche CoreBluetooth sans `NSBluetoothAlwaysUsageDescription` dans *son* Info.plist. Le launcher porte cette clé et lance Python en processus enfant, qui hérite de l'autorisation.
- **Protocole** repris octet pour octet de l'app officielle via [TiMini-Print](https://github.com/Dejniel/TiMini-Print), recoupé avec [phomymo](https://github.com/transcriptionstream/phomymo) et [phomemo-tools](https://github.com/vivier/phomemo-tools) : GATT `ff00/ff02/ff03`, `GS v 0` par blocs de 255 lignes, paquets de 128 o / 20 ms.
- **Tramage auto hybride** : seuil net sur le texte, Atkinson seulement sur les aplats gris (détection par filtre min/max 5×5, qui ignore l'anticrénelage des lettres). Atkinson plutôt que Floyd-Steinberg : plus contrasté, moins de points isolés, qui bavent sur papier thermique.
- **Barre de menus découplée** : l'app native lit un `status.json` publié par le pont et lui envoie des commandes `#T02:…` sur le socket local. Une seule autorisation Bluetooth (celle du pont), et le pont tourne même si l'icône est fermée.

## Si Apple coupe les drivers PPD

CUPS affiche déjà « *Printer drivers are deprecated* ». Le jour où `lpadmin -P` ne marche plus, la voie de sortie est de faire parler le pont en **IPP Everywhere** (macOS l'ajoute alors sans driver). Le cœur (raster PWG déjà supporté, tramage, BLE) ne change pas — seule la porte d'entrée change.

## Dev

```bash
python3 -m pytest -q tests/       # 26 tests, sans imprimante ni Bluetooth
python3 tools/gen_ppd.py          # régénère le PPD (ajoute tes formats dans SIZES)
python3 -m t02bridge dry-run job.ras -o out.bin   # octets bruts sans BLE
```

Testé : parsing raster v1/v2/v3 (big & little endian), octets du protocole, retry/spool, et une vraie chaîne CUPS (`lp` d'un PDF → PPD → socket → pont). Non testé ici : CoreBluetooth sur macOS 27 et la T02 physique — c'est le premier `install.sh --testpage` qui le dira.
