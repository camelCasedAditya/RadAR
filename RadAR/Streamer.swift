import ARKit
import CoreImage
import Foundation

/// Runs ARKit and streams to the relay: pose + H.264 video at 30 fps, and a 518-wide JPEG "map frame"
/// for the GPU only when the phone has moved enough to add geometry. All state lives on `queue`.
nonisolated final class Streamer: NSObject, ARSessionDelegate, @unchecked Sendable {
    let session = ARSession()
    var onStatus: (@Sendable (String) -> Void)?

    private let queue: DispatchQueue
    private let ci = CIContext()
    private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!
    private var ws: URLSessionWebSocketTask?
    private var base: URL?
    private var rid = "A"
    private var frameId: UInt32 = 0
    private let encoder = VideoEncoder()
    private var lastFrame: TimeInterval = 0
    private var lastMap: (time: TimeInterval, T: simd_float4x4)?
    private var pending = 0                  // socket sends not yet completed
    private var dropping = false             // network fell behind: skip video until the next keyframe
    private var videoTimes: [TimeInterval] = []
    private var mapTimes: [TimeInterval] = []
    private var tracking = "starting"
    private var link = "connecting"
    private var lastError = ""
    // AirPods extras (Airpods.swift), all optional
    let voice = VoiceCall()
    private let heart: HeartRate
    private let head: HeadTracker
    private var phoneYaw = 0.0                   // camera heading in the ARKit world frame, as the hub computes it
    private var bpm: Int?
    private var audioSeq: UInt32 = 0

    static let width = 518  // lingbot-map native width; height is rounded to a multiple of 14
    // lingbot-map is a streaming VIDEO model: each frame leans on the frames just before it, so it
    // needs a steady, overlapping stream (its demos use 10 fps). Sparse "only when moved 30 cm" input
    // collapsed its depth confidence. So: a fixed rate while moving, 1 fps when still.
    // ponytail: the GPU path returns ~10 frames/s total through the hotspot tunnel (2 in flight per phone
    // x ~0.4 s round trip), so 4 fps per phone keeps two phones from backing up. Voxel merging on the
    // relay means 4 vs 6 fps barely changes map density at walking pace.
    static let mapFps: Double = 4
    static let mapMove: Float = 0.02         // "still" = under 2 cm and 1 degree since the last map frame
    static let mapTurn: Float = 1 * .pi / 180
    static let mapFloor: TimeInterval = 1    // at least one map frame per second even when still

    override init() {
        let q = DispatchQueue(label: "radar.stream")
        queue = q
        heart = HeartRate(queue: q)
        head = HeadTracker(queue: q)
        super.init()
    }

    func start(url: URL, id: String) {
        queue.async { [self] in
            base = url; rid = id; frameId = 0; lastMap = nil
            encoder.onFrame = { [weak self] f, key, data in
                self?.queue.async { self?.sendVideo(f, key: key, data: data) }
            }
            connect()
            heart.onBPM = { [weak self] b in self?.bpm = Int(b.rounded()); self?.emit(.string(Wire.json(["t": "hr", "bpm": b]))) }
            head.phoneYaw = { [weak self] in self?.phoneYaw ?? 0 }
            head.onHead = { [weak self] yaw, pitch in self?.emit(.string(Wire.json(["t": "head", "yaw": yaw, "pitch": pitch]))) }
            voice.onChunk = { [weak self] pcm in
                self?.queue.async {
                    guard let self else { return }
                    self.audioSeq &+= 1
                    self.emit(.data(Wire.audioFrame(id: self.rid, seq: self.audioSeq, pcm: pcm)))
                }
            }
            heart.start()
            head.start()
            voice.start()
        }
        let cfg = ARWorldTrackingConfiguration()
        cfg.worldAlignment = .gravity
        // LiDAR phones (Pro): metric scene depth rides along with each map frame and the relay maps
        // from it directly, no GPU. Other phones keep sending plain JPEGs for lingbot.
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.smoothedSceneDepth) {
            cfg.frameSemantics.insert(.smoothedSceneDepth)
        }
        session.delegateQueue = queue
        session.delegate = self
        session.run(cfg, options: [.resetTracking, .removeExistingAnchors])
    }

    func stop() {
        session.pause()
        heart.stop()
        head.stop()
        voice.stop()
        queue.async { [self] in
            bpm = nil
            base = nil
            encoder.stop()
            ws?.cancel(with: .goingAway, reason: nil)
            ws = nil
        }
    }

    // MARK: socket

    private func connect() {
        guard let base else { return }
        let task = URLSession.shared.webSocketTask(with: base.appendingPathComponent("phone/\(rid)"))
        ws = task
        pending = 0
        dropping = true                      // new socket: start the video on a keyframe
        encoder.requestKeyframe()
        link = "connecting"
        task.resume()
        task.send(.string(#"{"t":"hello","name":"\#(rid)"}"#)) { [weak self] err in
            self?.queue.async { if err == nil { self?.link = "live" } }
        }
        listen(task)
    }

    /// The relay sends phones only call audio (kind 7) from the other phones and the hub.
    private func listen(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard self.ws === task else { return }
                switch result {
                case .failure(let e):
                    self.reconnect(because: "receive: \(e.localizedDescription)")
                case .success(let m):
                    if case .data(let d) = m, d.count > 6, d[d.startIndex] == 7 {
                        let b = [UInt8](d), idlen = Int(b[1])
                        if b.count > 6 + idlen {
                            self.voice.play(String(decoding: b[2..<2 + idlen], as: UTF8.self), Data(b[(6 + idlen)...]))
                        }
                    }
                    self.listen(task)
                }
            }
        }
    }

    /// Push-to-talk (ContentView's Hold to talk button).
    func setTalking(_ on: Bool) {
        voice.setTalking(on)
        emit(.string(Wire.json(["t": "talk", "on": on])))
    }

    /// Send from any thread, if connected.
    private func emit(_ m: URLSessionWebSocketTask.Message) {
        queue.async { [weak self] in
            guard let self, let ws = self.ws else { return }
            self.send(m, on: ws)
        }
    }

    private func reconnect(because why: String) {
        lastError = why
        print("[radar] socket dropped (\(why)); close code \(ws?.closeCode.rawValue ?? -1)")
        ws?.cancel()
        ws = nil
        link = "reconnecting"
        queue.asyncAfter(deadline: .now() + 1) { [weak self] in
            if self?.ws == nil { self?.connect() }
        }
    }

    // MARK: ARSessionDelegate (called on `queue`)

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        tracking = Self.describe(frame.camera.trackingState)
        guard let ws, frame.timestamp - lastFrame >= 1.0 / 30 - 0.004 else { return }   // ARKit may run at 60
        lastFrame = frame.timestamp

        let res = frame.camera.imageResolution
        let sx = CGFloat(Self.width) / res.width
        let h = Int((res.height * sx / 14).rounded()) * 14
        let sy = CGFloat(h) / res.height
        let k = frame.camera.intrinsics
        let K: [Float] = [k[0][0] * Float(sx), k[1][1] * Float(sy), k[2][0] * Float(sx), k[2][1] * Float(sy)]
        frameId &+= 1
        let f = frameId
        let T = frame.camera.transform
        phoneYaw = Double(atan2(T.columns.2.x, T.columns.2.z))   // camera -Z axis -> atan2(-fwd.x, -fwd.z)
        let mapFrame = isMapFrame(T, at: frame.timestamp) && pending < 30
        // ARKit's tracked feature points carry metric depth; the relay reads lingbot's depth at the same
        // pixels to get the map scale per frame (no camera travel needed). Only with map frames: ~300 x 3 numbers.
        let fp: [[Float]] = mapFrame ? Self.featurePoints(frame, K: K, w: Self.width, h: h) : []
        send(.string(Wire.pose(frame: f, ts: frame.timestamp, T: T, K: K, w: Self.width, h: h, fp: fp)), on: ws)

        if pending > 30 { dropping = true }          // ~1 s of sends backed up
        if !dropping || pending < 5 {
            encoder.encode(frame.capturedImage, time: frame.timestamp, frame: f)
        }

        if mapFrame {
            lastMap = (frame.timestamp, T)
            let img = CIImage(cvPixelBuffer: frame.capturedImage).transformed(by: CGAffineTransform(scaleX: sx, y: sy))
            if let jpeg = ci.jpegRepresentation(of: img, colorSpace: sRGB,
                                                options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6]) {
                if let d = frame.smoothedSceneDepth, let m = Wire.depthFrame(id: rid, frame: f, depth: d, jpeg: jpeg) {
                    send(.data(m), on: ws)
                } else {
                    send(.data(Wire.jpegFrame(id: rid, frame: f, jpeg: jpeg)), on: ws)
                }
                mapTimes.append(frame.timestamp)
            }
        }

        videoTimes.removeAll { frame.timestamp - $0 > 2 }
        mapTimes.removeAll { frame.timestamp - $0 > 2 }
        let airpods = (bpm.map { " · ♥ \($0)" } ?? "") + (head.connected ? " · head ✓" : "")
        onStatus?("\(link) · video \(videoTimes.count / 2) fps · map \(mapTimes.count / 2) fps · tracking \(tracking)\(airpods)"
                  + (lastError.isEmpty ? "" : "\nlast drop: \(lastError)"))
    }

    /// Feature points projected into the map image: [u, v, depth] with depth in metres along the camera axis.
    private static func featurePoints(_ frame: ARFrame, K: [Float], w: Int, h: Int) -> [[Float]] {
        guard let pts = frame.rawFeaturePoints?.points, !pts.isEmpty else { return [] }
        let w2c = frame.camera.transform.inverse
        var out: [[Float]] = []
        out.reserveCapacity(min(pts.count, 300))
        for p in pts.prefix(300) {
            let c = w2c * simd_float4(p, 1)
            let z = -c.z                                   // ARKit camera looks down -Z
            guard z > 0.2, z < 8 else { continue }
            let u = K[0] * c.x / z + K[2], v = K[1] * (-c.y) / z + K[3]   // OpenCV image axes (y down)
            guard u >= 0, u < Float(w), v >= 0, v < Float(h) else { continue }
            out.append([u, v, z])
        }
        return out
    }

    /// Time for the next map frame: at mapFps while the phone is moving at all, 1 fps when still.
    private func isMapFrame(_ T: simd_float4x4, at t: TimeInterval) -> Bool {
        guard let last = lastMap else { return true }
        if t - last.time < 1 / Self.mapFps - 0.004 { return false }
        if t - last.time >= Self.mapFloor { return true }
        let moved = simd_distance(simd_make_float3(T.columns.3), simd_make_float3(last.T.columns.3))
        let r = simd_mul(simd_transpose(simd_float3x3(simd_make_float3(last.T.columns.0), simd_make_float3(last.T.columns.1),
                                                      simd_make_float3(last.T.columns.2))),
                         simd_float3x3(simd_make_float3(T.columns.0), simd_make_float3(T.columns.1), simd_make_float3(T.columns.2)))
        let turned = acos(max(-1, min(1, (r[0][0] + r[1][1] + r[2][2] - 1) / 2)))
        return moved >= Self.mapMove || turned >= Self.mapTurn
    }

    private func sendVideo(_ f: UInt32, key: Bool, data: Data) {
        guard let ws else { return }
        if dropping {
            guard key else { encoder.requestKeyframe(); return }   // resume only on a keyframe
            dropping = false
        }
        send(.data(Wire.h264Frame(id: rid, frame: f, keyframe: key, annexB: data)), on: ws)
        videoTimes.append(lastFrame)
    }

    private func send(_ m: URLSessionWebSocketTask.Message, on ws: URLSessionWebSocketTask) {
        pending += 1
        ws.send(m) { [weak self] err in
            self?.queue.async {
                guard let self else { return }
                self.pending -= 1
                if let err, self.ws === ws { self.reconnect(because: "send: \(err.localizedDescription)") }
            }
        }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        onStatus?("AR error: \(error.localizedDescription)")
    }

    private static func describe(_ s: ARCamera.TrackingState) -> String {
        switch s {
        case .normal: return "normal"
        case .notAvailable: return "unavailable"
        case .limited(let r):
            switch r {
            case .initializing: return "initializing"
            case .excessiveMotion: return "too fast"
            case .insufficientFeatures: return "low detail"
            case .relocalizing: return "relocalizing"
            @unknown default: return "limited"
            }
        }
    }
}
