// Phomemo T02 — menu-bar status item.
//
// Plain AppKit (NSStatusItem), no third-party dependency. It never touches
// Bluetooth: it reads status.json published by the bridge, and sends tiny
// "#T02:<command>" messages to the bridge over its local socket (same door
// CUPS uses). Built by install.sh:
//   swiftc -O -o T02Menu main.swift -framework AppKit -framework Network

import AppKit
import Network

// MARK: - Paths & constants

let home = FileManager.default.homeDirectoryForCurrentUser
let appDir = home.appendingPathComponent("Library/Application Support/PhomemoT02")
let statusURL = appDir.appendingPathComponent("status.json")
let configURL = appDir.appendingPathComponent("config.json")
let failedDir = appDir.appendingPathComponent("failed")
let pythonURL = appDir.appendingPathComponent("venv/bin/python3")
let logURL = home.appendingPathComponent("Library/Logs/PhomemoT02/bridge.log")
let bridgeLabel = "net.clawfire.t02bridge"
let staleAfter: TimeInterval = 90  // the bridge heartbeats every 30 s

// MARK: - Bridge status (mirror of status.json)

struct BridgeStatus {
    var state = "unknown"
    var detail = ""
    var connected = false
    var battery: Int?
    var batteryAt: Date?
    var paper: String?
    var queue = 0
    var failed = 0
    var lastJobAt: Date?
    var lastJobOK: Bool?
    var lastError: String?
    var updated = Date.distantPast
    var version = "?"

    var isAlive: Bool { Date().timeIntervalSince(updated) < staleAfter }

    static func load() -> BridgeStatus? {
        guard let data = try? Data(contentsOf: statusURL),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let o = obj as? [String: Any] else { return nil }
        func date(_ key: String) -> Date? {
            (o[key] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        }
        var s = BridgeStatus()
        s.state = o["state"] as? String ?? "unknown"
        s.detail = o["detail"] as? String ?? ""
        s.connected = (o["connected"] as? NSNumber)?.boolValue ?? false
        s.battery = (o["battery"] as? NSNumber)?.intValue
        s.batteryAt = date("battery_at")
        s.paper = o["paper"] as? String
        s.queue = (o["queue"] as? NSNumber)?.intValue ?? 0
        s.failed = (o["failed"] as? NSNumber)?.intValue ?? 0
        s.lastJobAt = date("last_job_at")
        s.lastJobOK = (o["last_job_ok"] as? NSNumber)?.boolValue
        s.lastError = o["last_error"] as? String
        s.updated = date("updated") ?? .distantPast
        s.version = o["version"] as? String ?? "?"
        return s
    }
}

func configuredPort() -> UInt16 {
    guard let data = try? Data(contentsOf: configURL),
          let obj = try? JSONSerialization.jsonObject(with: data),
          let o = obj as? [String: Any],
          let port = (o["port"] as? NSNumber)?.uint16Value else { return 9100 }
    return port
}

// MARK: - Talking to the bridge

/// Send "#T02:<name>" to the bridge socket; it closes the connection once queued.
func sendCommand(_ name: String) {
    guard let port = NWEndpoint.Port(rawValue: configuredPort()) else { return }
    let conn = NWConnection(host: "127.0.0.1", port: port, using: .tcp)
    conn.stateUpdateHandler = { state in
        switch state {
        case .ready:
            let payload = "#T02:\(name)\n".data(using: .utf8)
            conn.send(content: payload, contentContext: .finalMessage, isComplete: true,
                      completion: .contentProcessed { _ in
                conn.receive(minimumIncompleteLength: 1, maximumLength: 1) { _, _, _, _ in
                    conn.stateUpdateHandler = nil  // break the retain cycle
                    conn.cancel()
                }
            })
        case .failed(_), .waiting(_):  // bridge not listening
            conn.stateUpdateHandler = nil
            conn.cancel()
        default:
            break
        }
    }
    conn.start(queue: .global(qos: .utility))
}

@discardableResult
func run(_ path: String, _ args: [String], env extra: [String: String] = [:]) -> Process? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    var env = ProcessInfo.processInfo.environment
    extra.forEach { env[$0.key] = $0.value }
    p.environment = env
    do { try p.run(); return p } catch { NSLog("T02Menu: cannot run \(path): \(error)"); return nil }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private var timer: Timer?
    private let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()
    private let clock: DateFormatter = {
        let f = DateFormatter()
        f.timeStyle = .short
        f.dateStyle = .none
        return f
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        refreshIcon()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.refreshIcon()
        }
    }

    // Icon: printer when fine, filled while working, warning when something needs you.
    private func refreshIcon() {
        let s = BridgeStatus.load()
        var symbol = "printer"
        var tip = "Phomemo T02"
        if let s = s, s.isAlive {
            switch s.state {
            case "printing", "checking", "connecting":
                symbol = "printer.fill"; tip = "Phomemo T02 — impression…"
            case "paper_out", "waiting":  // a job is stuck — "offline" alone is just a sleeping T02
                symbol = "exclamationmark.triangle"; tip = "Phomemo T02 — \(s.detail)"
            default:
                if s.failed > 0 { symbol = "exclamationmark.triangle"; tip = "Phomemo T02 — \(s.failed) job(s) raté(s)" }
            }
        } else {
            symbol = "exclamationmark.triangle"; tip = "Phomemo T02 — le pont ne répond pas"
        }
        guard let button = item.button else { return }
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: tip) {
            img.isTemplate = true
            button.image = img
            button.title = ""
        } else {
            button.image = nil
            button.title = "T02"
        }
        button.toolTip = tip
    }

    // Rebuilt every time the menu opens, so it is always fresh.
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let s = BridgeStatus.load()

        guard let st = s, st.isAlive else {
            info("⚠︎ Le pont d'impression ne répond pas")
            menu.addItem(.separator())
            action("Redémarrer le pont", #selector(restartBridge))
            action("Afficher le journal", #selector(openLog))
            menu.addItem(.separator())
            action("Quitter", #selector(quit), key: "q")
            return
        }

        info("Phomemo T02 — \(stateLabel(st))")
        if !st.detail.isEmpty, st.state != "idle" { info("   \(st.detail)") }
        if let b = st.battery {
            let when = st.batteryAt.map { " · " + relative.localizedString(for: $0, relativeTo: Date()) } ?? ""
            info("Batterie : \(b) %\(when)")
        } else {
            info("Batterie : inconnue (« Vérifier l'imprimante »)")
        }
        if let paper = st.paper { info(paper == "ok" ? "Papier : OK" : "Papier : ⚠︎ vide") }
        info("File : \(st.queue) en attente · \(st.failed) raté(s)")
        if let at = st.lastJobAt {
            let mark = st.lastJobOK == false ? "✗" : "✓"
            info("Dernier job : \(clock.string(from: at)) \(mark)")
        }
        if let err = st.lastError, st.state != "idle" || st.lastJobOK == false {
            info("   \(err.prefix(70))")
        }

        menu.addItem(.separator())
        action("Vérifier l'imprimante", #selector(checkPrinter))
        action("Imprimer une page de test", #selector(testPage))
        let retry = action("Relancer les jobs ratés (\(st.failed))", #selector(retryFailed))
        retry.isEnabled = st.failed > 0
        if st.failed > 0 { action("Ouvrir le dossier des jobs ratés", #selector(openFailed)) }

        menu.addItem(.separator())
        action("Afficher le journal", #selector(openLog))
        action("Réglages (config.json)…", #selector(openConfig), key: ",")
        action("Redémarrer le pont", #selector(restartBridge))
        menu.addItem(.separator())
        info("Pont v\(st.version)")
        action("Quitter", #selector(quit), key: "q")
    }

    private func stateLabel(_ s: BridgeStatus) -> String {
        switch s.state {
        case "idle": return s.connected ? "prête (connectée)" : "prête"
        case "printing": return "impression en cours"
        case "checking", "connecting": return "connexion…"
        case "paper_out": return "plus de papier"
        case "waiting": return "en attente de l'imprimante"
        case "offline": return "injoignable (éteinte ?)"
        default: return s.state
        }
    }

    private func info(_ title: String) {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        menu.addItem(i)
    }

    @discardableResult
    private func action(_ title: String, _ sel: Selector, key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self
        menu.addItem(i)
        return i
    }

    // MARK: actions

    @objc private func checkPrinter() { sendCommand("status") }
    @objc private func testPage() { sendCommand("testpage") }

    @objc private func retryFailed() {
        run(pythonURL.path, ["-m", "t02bridge", "retry"],
            env: ["PYTHONPATH": appDir.appendingPathComponent("app").path])
    }

    @objc private func openFailed() {
        try? FileManager.default.createDirectory(at: failedDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(failedDir)
    }

    @objc private func openLog() {
        let console = URL(fileURLWithPath: "/System/Applications/Utilities/Console.app")
        NSWorkspace.shared.open([logURL], withApplicationAt: console,
                                configuration: NSWorkspace.OpenConfiguration(),
                                completionHandler: nil)
    }

    @objc private func openConfig() {
        if !FileManager.default.fileExists(atPath: configURL.path) {
            try? "{}\n".write(to: configURL, atomically: true, encoding: .utf8)
        }
        run("/usr/bin/open", ["-t", configURL.path])
    }

    @objc private func restartBridge() {
        run("/bin/launchctl", ["kickstart", "-k", "gui/\(getuid())/\(bridgeLabel)"])
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
