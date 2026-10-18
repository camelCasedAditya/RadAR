import ARKit
import Foundation
@preconcurrency import NearbyInteraction

/// Distance and direction to the other responders over UWB (Nearby Interaction), phone to phone, with
/// no shared start mark needed. One NISession per other phone; the sessions' discovery tokens travel
/// through the relay (`phones`, `nitoken` in server/relay/protocol.md). With camera assistance (iPhone 14
/// and later need it for direction) the peer also gets a position in this phone's ARKit world.
/// All calls and delegate callbacks run on Streamer's queue.
nonisolated final class UwbTracker: NSObject, NISessionDelegate, @unchecked Sendable {
    var me = ""
    var sendToken: ((_ to: String, _ token: String) -> Void)?
    var onReport: ((_ peer: String, _ distance: Double, _ angle: Double?) -> Void)?
    var cameraT: () -> simd_float4x4? = { nil }
    private(set) var latest: [String: (distance: Double, angle: Double?, at: TimeInterval)] = [:]
    private let queue: DispatchQueue
    private let ar: ARSession
    private var phones: Set<String> = []
    private var sessions: [String: NISession] = [:]
    private var configs: [String: NINearbyPeerConfiguration] = [:]
    private var lastToken: [String: String] = [:]       // last token received from each peer
    private var lastReport: [String: TimeInterval] = [:]
    private var state: [String: String] = [:]           // per peer, for the phone's status line
    private var noAssist = false                        // camera assistance failed once: measure without it
    private var lastReopen: [String: TimeInterval] = [:]
    private var tokenTries: [String: Int] = [:]

    /// e.g. "uwb B 3.2 m ↗ · C waiting for token" (shown on the phone so a stalled pairing is visible)
    var status: String {
        guard Self.supported else { return "uwb: not supported on this phone" }
        guard !phones.isEmpty else { return "uwb: no other phones" }
        let now = ProcessInfo.processInfo.systemUptime
        return "uwb " + phones.sorted().map { id in
            if let l = latest[id], now - l.at < 3 { return "\(id) \(String(format: "%.1f", l.distance)) m\(l.angle == nil ? " (no direction)" : "")" }
            return "\(id) \(state[id] ?? "starting")"
        }.joined(separator: " · ")
    }

    static var supported: Bool { NISession.deviceCapabilities.supportsPreciseDistanceMeasurement }
    private static var cameraAssist: Bool { NISession.deviceCapabilities.supportsCameraAssistance }

    init(queue: DispatchQueue, ar: ARSession) {
        self.queue = queue
        self.ar = ar
    }

    /// The relay's phone list: pair with every other phone, drop the ones that left.
    func setPhones(_ ids: [String]) {
        guard Self.supported else { return }
        phones = Set(ids).subtracting([me])
        for id in phones where sessions[id] == nil { open(id) }
        for id in sessions.keys where !phones.contains(id) { close(id) }
    }

    /// A peer's token for its session with us. A new token (the peer opened a fresh session) also gets
    /// ours sent back, since that new session has not seen it; an unchanged one does not, so the
    /// exchange ends after one round trip.
    func received(token b64: String, from id: String) {
        guard Self.supported, b64 != lastToken[id], let data = Data(base64Encoded: b64),
              let token = try? NSKeyedUnarchiver.unarchivedObject(ofClass: NIDiscoveryToken.self, from: data) else { return }
        lastToken[id] = b64
        let s = sessions[id] ?? open(id, announce: false)
        sendMyToken(s, to: id)
        let cfg = NINearbyPeerConfiguration(peerToken: token)
        if Self.cameraAssist && !noAssist { cfg.isCameraAssistanceEnabled = true }
        state[id] = "searching"
        configs[id] = cfg
        s.run(cfg)
    }

    func stop() {
        for id in Array(sessions.keys) { close(id) }
        phones = []
    }

    @discardableResult
    private func open(_ id: String, announce: Bool = true) -> NISession {
        let s = Self.makeSession()
        s.delegate = self
        s.delegateQueue = queue
        if Self.cameraAssist && !noAssist { s.setARSession(ar) }   // shares the streaming ARSession
        sessions[id] = s
        state[id] = "waiting for their token"
        if announce { sendMyToken(s, to: id) }
        return s
    }

    private func close(_ id: String) {
        sessions.removeValue(forKey: id)?.invalidate()
        configs[id] = nil
        lastToken[id] = nil
        latest[id] = nil
        state[id] = nil
    }

    /// NISession on the main thread: its discovery token came back nil when made on our background queue.
    private static func makeSession() -> NISession {
        if Thread.isMainThread { return NISession() }
        nonisolated(unsafe) var s: NISession?
        DispatchQueue.main.sync { s = NISession() }
        return s!
    }

    /// The token can still be nil right after the session is made: retry every 0.5 s. After 2 s with
    /// camera assistance on, start over without it (sharing the ARSession is the likely blocker).
    private func sendMyToken(_ s: NISession, to id: String) {
        guard let t = s.discoveryToken,
              let data = try? NSKeyedArchiver.archivedData(withRootObject: t, requiringSecureCoding: true) else {
            let n = (tokenTries[id] ?? 0) + 1
            tokenTries[id] = n
            state[id] = "no token yet (try \(n)\(noAssist || !Self.cameraAssist ? ", no camera" : ""))"
            if n == 4, Self.cameraAssist, !noAssist {
                noAssist = true
                let cfg = configs[id]
                close(id)
                let fresh = open(id)                    // announces itself (sendMyToken) again
                if let cfg { cfg.isCameraAssistanceEnabled = false; configs[id] = cfg; fresh.run(cfg) }
            } else if n < 20 {
                queue.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    if let self, self.sessions[id] === s { self.sendMyToken(s, to: id) }
                }
            }
            return
        }
        tokenTries[id] = nil
        sendToken?(id, data.base64EncodedString())
    }

    private func peer(of s: NISession) -> String? { sessions.first { $0.value === s }?.key }

    // MARK: NISessionDelegate (on `queue`)

    func session(_ session: NISession, didUpdate nearbyObjects: [NINearbyObject]) {
        guard let id = peer(of: session), let o = nearbyObjects.first, let d = o.distance else { return }
        var angle: Double?
        if let wt = session.worldTransform(for: o), let T = cameraT() {
            // camera-assisted: the peer's ARKit position, measured against this camera's heading
            let rel = simd_make_float3(wt.columns.3) - simd_make_float3(T.columns.3)
            let f = simd_normalize(simd_float3(-T.columns.2.x, 0, -T.columns.2.z)), right = simd_float3(-f.z, 0, f.x)
            angle = Double(atan2(simd_dot(rel, right), simd_dot(rel, f)))
        } else if let dir = o.direction {
            angle = Double(atan2(dir.x, -dir.z))       // device axes: x right, -z out of the back camera
        }
        let now = ProcessInfo.processInfo.systemUptime
        latest[id] = (Double(d), angle, now)
        if now - (lastReport[id] ?? 0) >= 0.2 {         // 5 Hz to the hub
            lastReport[id] = now
            onReport?(id, Double(d), angle)
        }
    }

    func session(_ session: NISession, didRemove nearbyObjects: [NINearbyObject], reason: NINearbyObject.RemovalReason) {
        guard let id = peer(of: session) else { return }
        latest[id] = nil
        state[id] = reason == .timeout ? "out of range" : "they stopped"
        if reason == .timeout, let cfg = configs[id] { session.run(cfg) }   // out of range for a while: keep trying
    }

    func sessionSuspensionEnded(_ session: NISession) {
        if let id = peer(of: session), let cfg = configs[id] { session.run(cfg) }
    }

    func session(_ session: NISession, didInvalidateWith error: Error) {
        guard let id = peer(of: session) else { return }
        print("[radar] uwb \(id): \(error)")
        let code = (error as? NIError)?.code
        if Self.cameraAssist && !noAssist && code != .userDidNotAllow { noAssist = true }   // retry without the camera
        close(id)
        state[id] = "error: \(error.localizedDescription)"
        let now = ProcessInfo.processInfo.systemUptime
        guard phones.contains(id), code != .userDidNotAllow, now - (lastReopen[id] ?? 0) > 5 else { return }
        lastReopen[id] = now                            // re-pair with a fresh session and token, at most every 5 s
        open(id)
    }
}
