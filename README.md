# Phomemo T02 → macOS printer

**English** · [Français](README.fr.md)

Your Phomemo T02 shows up in **System Settings › Printers** and in **⌘P** like any other printer. No vendor app, no phone. Conversion and Bluetooth delivery happen **in the background**: the macOS print queue empties instantly, and the bridge takes it from there (retrying while the T02 is asleep).

```
App (⌘P) ─► macOS renders the page ─► CUPS ─► socket://127.0.0.1:9100
                                                    │
                             Phomemo T02 Bridge (LaunchAgent, Python)
                             spool ► 384-dot dithering ► T02 ESC/POS ► BLE ─► 🖨
```

> The print-dialog options and the menu-bar app are labelled in French for now. Translations welcome.

## Install

```bash
brew install python          # Python ≥ 3.10 (or pyenv); Apple's python3 is too old
xcode-select --install       # if clang/swiftc are missing
./install.sh --testpage      # switch the T02 on first
```

During the install, macOS asks for Bluetooth access for **"Phomemo T02 Bridge"** → *Allow*. Then an admin password, to add the CUPS queue. That's it.

Re-running `install.sh` updates or repairs everything (it's idempotent). The launcher is only rebuilt when it changes, so the Bluetooth permission survives updates. `./uninstall.sh [--purge]` removes it all.

## Usage

- **⌘P → Phomemo T02**. The default paper size is **"Ticket 48 mm"**: blank space at the top and bottom is trimmed automatically, so the strip is exactly as long as the content.
- Fixed `48 × 30…200 mm` sizes for labels, or a custom size (width ≤ 48 mm).
- Options in the print sheet (*Printer Features*):

| Option | Choices |
|---|---|
| Density | Light · **Normal** · Dark · Darkest |
| Dithering | **Auto** (crisp text + dithered greys) · Floyd–Steinberg · Threshold · Atkinson |
| Feed after job | None · +5 / +10 / +15 mm |
| Trim blank space | **Yes** · No (exact size, for labels) |

- T02 switched off? The job waits in the spool and is retried for ~9 min, with a macOS notification if needed. Out of paper or cover open → notification.
- Browser previews show the whole page before trimming, so a short label can look centred on a long strip. The actual print only contains the content.

## Menu bar

A 🖨 icon (native app, no dependency) shows at a glance:

- **status**: ready, printing, out of paper, waiting. The ⚠︎ only appears when a job is stuck or has failed; a switched-off T02 is normal;
- **battery and paper**, as reported by the last job or the last check;
- **queue**: pending jobs, failed jobs, last job.

Actions: *Check printer*, *Print a test page*, *Retry failed jobs*, log, settings, restart the bridge.

The app never touches Bluetooth: it reads `status.json` and talks to the bridge through its local socket. *Quit* closes it until the next login. If it failed to build, details are in `~/Library/Logs/PhomemoT02/menubar-build.log`; printing doesn't depend on it.

## Handy commands

```bash
B="$HOME/Library/Application Support/PhomemoT02"
alias t02='PYTHONPATH="$B/app" "$B/venv/bin/python3" -m t02bridge'

t02 scan --save              # find / pin your T02
t02 print photo.jpg          # print directly, without CUPS
t02 testpage --density 3
t02 status                   # bridge and printer state
t02 status --ping            # … after querying the T02 (battery, paper)
t02 retry --list             # failed jobs (kept 7 days)
t02 retry                    # … handed back to the bridge, which prints them
tail -f ~/Library/Logs/PhomemoT02/bridge.log
```

Defaults live in `~/Library/Application Support/PhomemoT02/config.json`:

| Key | Default | Purpose |
|---|---|---|
| `density`, `dither`, `trim` | `2`, `"auto"`, `true` | used when the app doesn't choose |
| `keepalive_seconds` | `60` | keep the BLE link open between jobs (`0` = disconnect every time) |
| `failed_retention_days` | `7` | max age of failed jobs (`0` = keep forever) |
| `photo_dither` | `"atkinson"` | how Auto mode dithers grey areas (`"floyd"` for the older look) |
| `printer_name`, `port` | `"T02"`, `9100` | Bluetooth name, local port |

After editing: `launchctl kickstart -k gui/$(id -u)/net.clawfire.t02bridge`.

**Logs**: `bridge.log` rotates on its own (3 × 1 MB). `launchd.log`, next to it, only catches crashes.

> ⚠️ For `t02 scan/print` run from Terminal, it's **Terminal** that needs Bluetooth access (System Settings › Privacy & Security › Bluetooth). The bridge uses its own permission.

## Design choices (the "why")

- **No custom CUPS filter or backend.** `/usr/libexec/cups` is protected by SIP. The PPD declares CUPS raster as the final format (`cupsFilter … -`): macOS renders the PDF with its own filter, and the built-in `socket://` backend hands the stream to the bridge. PPD options travel inside the raster header (`cupsInteger0-3`).
- **A tiny C launcher inside a `.app`.** macOS kills any process that touches CoreBluetooth without an `NSBluetoothAlwaysUsageDescription` in *its own* Info.plist. The launcher carries that key and starts Python as a child process, which inherits the permission.
- **Protocol** taken byte for byte from the official app via [TiMini-Print](https://github.com/Dejniel/TiMini-Print), cross-checked with [phomymo](https://github.com/transcriptionstream/phomymo) and [phomemo-tools](https://github.com/vivier/phomemo-tools): GATT `ff00/ff02/ff03`, `GS v 0` in 255-row blocks, 128-byte packets every 20 ms.
- **Hybrid auto dithering**: a hard threshold for text, Atkinson only on flat grey areas (found with a 5×5 min/max filter that ignores glyph anti-aliasing). Atkinson rather than Floyd–Steinberg: more contrast, fewer isolated dots, which smear on thermal paper.
- **Decoupled menu bar**: the native app reads a `status.json` published by the bridge and sends it `#T02:…` commands over the local socket. One Bluetooth permission (the bridge's), and the bridge keeps running even when the icon is closed.

## If Apple drops PPD drivers

CUPS already warns that *printer drivers are deprecated*. If `lpadmin -P` stops working one day, the way out is to have the bridge speak **IPP Everywhere** (macOS then adds it driverless). The core (PWG raster already supported, dithering, BLE) stays the same; only the front door changes.

## Development

```bash
python3 -m pytest -q tests/       # 26 tests, no printer or Bluetooth needed
python3 tools/gen_ppd.py          # regenerate the PPD (add your sizes to SIZES)
python3 -m t02bridge dry-run job.ras -o out.bin   # raw printer bytes, no BLE
```

Tested: raster parsing v1/v2/v3 (big & little endian), protocol bytes, retry/spool, and a real CUPS chain (`lp` of a PDF → PPD → socket → bridge). In the field: a T02 on macOS 27.0.1 (Apple Silicon), printing Spoolman labels from Chrome.

## Credits & licence

The T02 protocol comes from the reverse-engineering work of [TiMini-Print](https://github.com/Dejniel/TiMini-Print) (Dejniel), [phomymo](https://github.com/transcriptionstream/phomymo) (transcriptionstream) and [phomemo-tools](https://github.com/vivier/phomemo-tools) (Laurent Vivier). No code was copied, but none of this would exist without them.

Not affiliated with Phomemo. Code under the [MIT](LICENSE) licence.
