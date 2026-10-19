// 배터리 관리자 — 네이티브 셸 (WKWebView + 최적화 브리지)
import Cocoa
import WebKit

let fm = FileManager.default
let base = fm.homeDirectoryForCurrentUser.appendingPathComponent(".config/battman")

func runBootstrap() {
    guard let res = Bundle.main.resourcePath else { return }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = [res + "/battman/bootstrap.sh"]
    var env = ProcessInfo.processInfo.environment
    env["BATTMAN_NO_OPEN"] = "1"
    p.environment = env
    try? p.run()
    p.waitUntilExit()
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!

    struct Job: Decodable { let domain: String; let label: String }
    struct Item: Decodable {
        let kind: String
        let pid: Int
        let comm: String
        let exe: String
        let cpu: Double
        let rate: Double?
        let admin: Bool
        let checked: Bool
        let why: String
        let job: Job?
    }
    struct Proc: Decodable { let comm: String; let cpu: Double }
    struct Power: Decodable { let now_w: Double; let usual_w: Double? }
    struct ScanResult: Decodable {
        let items: [Item]?
        let busy: [Proc]?
        let apps: [Proc]?
        let power: Power?
        let top: [Proc]?
        let total_cpu: Double?
        let window: Double?
        let error: String?
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        let rect = NSRect(x: 0, y: 0, width: 1140, height: 800)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "배터리 관리자"
        window.center()
        window.setFrameAutosaveName("BattmanMain")
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "battman")
        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        window.contentView!.addSubview(webView)
        let waitCSS = "display:flex;height:90vh;align-items:center;justify-content:center;" +
            "font-family:'Apple SD Gothic Neo',sans-serif;color:#8a8a8e;background:#1c1c1e"
        webView.loadHTMLString(
            "<meta charset='utf-8'><body style=\"\(waitCSS)\"><h2>🔋 데이터 분석 중…</h2></body>", baseURL: nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refresh(nil)
    }

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }
        switch action {
        case "optimize":
            optimize()
        case "kill":
            if let pid = body["pid"] as? Int, let name = body["name"] as? String,
               let exe = body["exe"] as? String {
                confirmKill(pid: pid, name: name, exe: exe)
            }
        default:
            break
        }
    }

    final class Box<T>: @unchecked Sendable { var value: T; init(_ v: T) { value = v } }

    /// optimize.py 실행 → (stdout, stderr). 실패·시간 초과를 빈 결과로 삼키지 않는다.
    func tool(_ args: [String], timeout: TimeInterval) -> (out: String, err: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [base.appendingPathComponent("optimize.py").path] + args
        let outPipe = Pipe(), errPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = errPipe
        do { try p.run() } catch { return ("", "python3 실행 실패: \(error.localizedDescription)") }
        // stderr 를 따로 읽어야 한쪽 파이프가 가득 차서 서로 기다리는 교착이 안 생긴다
        let errData = Box(Data()), errDone = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            errData.value = errPipe.fileHandleForReading.readDataToEndOfFile()
            errDone.signal()
        }
        let timedOut = Box(false)
        let killer = DispatchWorkItem {
            if p.isRunning { timedOut.value = true; p.terminate() }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        let out = outPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        killer.cancel()
        errDone.wait()
        if timedOut.value { return ("", "\(Int(timeout))초 안에 끝나지 않아 중단했습니다.") }
        return (String(data: out, encoding: .utf8) ?? "", String(data: errData.value, encoding: .utf8) ?? "")
    }

    func resetButton() {
        webView.evaluateJavaScript("window.optReset && window.optReset()", completionHandler: nil)
    }

    func optimize() {
        DispatchQueue.global(qos: .userInitiated).async {
            let res = self.tool(["--scan"], timeout: 60)
            DispatchQueue.main.async {
                self.resetButton()
                self.afterScan(res.out, res.err)
            }
        }
    }

    func pct(_ v: Double) -> String { "\(Int(v.rounded()))%" }

    func afterScan(_ out: String, _ err: String) {
        let alert = NSAlert()
        guard let r = try? JSONDecoder().decode(ScanResult.self, from: Data(out.utf8)),
              r.error == nil, let items = r.items else {
            let detail = (try? JSONDecoder().decode(ScanResult.self, from: Data(out.utf8)))?.error
                ?? (err.isEmpty ? out : err)
            alert.alertStyle = .warning
            alert.messageText = "분석 실패 ⚠️"
            alert.informativeText = String(detail.trimmingCharacters(in: .whitespacesAndNewlines).prefix(600))
            alert.runModal()
            return
        }
        let busy = r.busy ?? []
        let busyLine = busy.map { "\($0.comm) \(pct($0.cpu))" }.joined(separator: ", ")
        let measured = "\(Int((r.window ?? 0).rounded()))초 실측, 전체 CPU \(pct(r.total_cpu ?? 0))"
        var context = ""
        if let p = r.power {
            context += "\n🔋 지금 소모 \(Int(p.now_w.rounded()))W"
            if let u = p.usual_w { context += " · 평소 \(Int(u.rounded()))W (최근 7일 배터리 사용 기준)" }
        }
        if let apps = r.apps, !apps.isEmpty {
            context += "\n📱 쓰고 있는 앱이라 건드리지 않음: "
                + apps.map { "\($0.comm) \(pct($0.cpu))" }.joined(separator: ", ")
        }

        if items.isEmpty {
            if busy.isEmpty {
                alert.messageText = "뒤에서 쓸데없이 도는 건 없습니다 ✅"
                var info = measured + "." + context
                if let top = r.top, !top.isEmpty {
                    info += "\n지금 제일 많이 쓰는 것: " + top.map { "\($0.comm) \(pct($0.cpu))" }
                        .joined(separator: ", ")
                }
                alert.informativeText = info
            } else {
                alert.messageText = "끌 수 있는 범인은 못 찾았습니다"
                alert.informativeText = "macOS 시스템 프로세스가 CPU를 많이 쓰는 중입니다:\n"
                    + busy.map { "· \($0.comm) \(pct($0.cpu))" }.joined(separator: "\n")
                    + "\n\n시스템 프로세스라 강제로 끄지 않습니다. 이걸 깨우는 백그라운드 프로그램을 "
                    + "못 찾았으니, 오래 계속되면 재부팅을 권장합니다.\n" + context + "\n(\(measured))"
            }
            alert.runModal()
            return
        }

        alert.messageText = "뒤에서 배터리 먹는 것 \(items.count)개 발견"
        var info = "창 없이 백그라운드에서 도는 것만 골랐습니다. 쓰는 앱은 건드리지 않습니다."
        if items.contains(where: { $0.admin }) { info += "\n🔒 표시는 관리자 암호를 한 번 물어봅니다." }
        info += context
        if !busyLine.isEmpty { info += "\n\n지금 바쁜 시스템 프로세스: \(busyLine)" }
        alert.informativeText = info

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        var boxes: [NSButton] = []
        for it in items {
            let load = it.kind == "storm" ? "초당 \(Int((it.rate ?? 0).rounded()))개 실행" : "CPU \(pct(it.cpu))"
            let title = "\(it.admin ? "🔒 " : "")\(it.comm)  (PID \(it.pid), \(load))"
            let box = NSButton(checkboxWithTitle: title, target: nil, action: nil)
            box.state = it.checked ? .on : .off
            box.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
            let why = NSTextField(wrappingLabelWithString: it.why)
            why.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            why.textColor = .secondaryLabelColor
            why.preferredMaxLayoutWidth = 400
            stack.addArrangedSubview(box)
            stack.addArrangedSubview(why)
            stack.setCustomSpacing(10, after: why)
            boxes.append(box)
        }
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.widthAnchor.constraint(equalToConstant: 420).isActive = true
        stack.layoutSubtreeIfNeeded()
        stack.frame = NSRect(origin: .zero, size: stack.fittingSize)
        alert.accessoryView = stack
        alert.addButton(withTitle: "선택한 것 정리")
        alert.addButton(withTitle: "취소")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let chosen = zip(items, boxes).filter { $0.1.state == .on }
            .map { ["pid": $0.0.pid, "exe": $0.0.exe, "comm": $0.0.comm] as [String: Any] }
        applyAndReport(chosen)
    }

    /// 대시보드 버튼과 최적화 버튼이 같은 정리 경로(전체 경로 재확인·서비스 중지·관리자 권한)를 쓴다.
    func applyAndReport(_ chosen: [[String: Any]]) {
        guard !chosen.isEmpty,
              let data = try? JSONSerialization.data(withJSONObject: chosen),
              let json = String(data: data, encoding: .utf8) else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            let res = self.tool(["--apply", json], timeout: 700)  // 관리자 암호 입력 대기 포함
            DispatchQueue.main.async {
                self.refresh(nil)
                let done = NSAlert()
                done.messageText = "정리 결과"
                let text = res.out.trimmingCharacters(in: .whitespacesAndNewlines)
                done.informativeText = text.isEmpty ? "실행 실패: \(res.err)" : text
                done.runModal()
            }
        }
    }

    func confirmKill(pid: Int, name: String, exe: String) {
        let alert = NSAlert()
        alert.messageText = "\(name) 종료할까요?"
        alert.informativeText = "PID \(pid) 프로세스를 종료합니다.\n직접 실행한 작업이라면 취소하세요."
        alert.addButton(withTitle: "종료")
        alert.addButton(withTitle: "취소")
        if alert.runModal() == .alertFirstButtonReturn {
            applyAndReport([["pid": pid, "exe": exe, "comm": name]])
        }
    }

    @objc func refresh(_ sender: Any?) {
        DispatchQueue.global(qos: .userInitiated).async {
            runBootstrap()
            DispatchQueue.main.async {
                let html = base.appendingPathComponent("report.html")
                if fm.fileExists(atPath: html.path) {
                    self.webView.loadFileURL(html, allowingReadAccessTo: base)
                } else {
                    self.webView.loadHTMLString(
                        "<meta charset='utf-8'><h2>report.html 생성 실패 — launchd.log 확인</h2>", baseURL: nil)
                }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)

let mainMenu = NSMenu()
let appItem = NSMenuItem()
mainMenu.addItem(appItem)
let appMenu = NSMenu()
appMenu.addItem(NSMenuItem(title: "새로고침", action: #selector(AppDelegate.refresh(_:)), keyEquivalent: "r"))
appMenu.addItem(NSMenuItem(title: "창 닫기", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
appMenu.addItem(NSMenuItem.separator())
appMenu.addItem(NSMenuItem(title: "배터리 관리자 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
appItem.submenu = appMenu
app.mainMenu = mainMenu
app.run()
